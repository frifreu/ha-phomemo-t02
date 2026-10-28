"""Bluetooth-LE-Verbindung zum Phomemo T02 über den HA-Bluetooth-Stack.

Bevorzugt ESPHome-Bluetooth-Proxys: Manche T02 trennen Verbindungen über den
lokalen BlueZ-Adapter des HA-Rechners nach wenigen Zehntelsekunden. Ist kein
Proxy in Reichweite, wird HAs normale Adapterwahl genutzt.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CHUNK_DELAY, CHUNK_SIZE, NOTIFY_UUID, PRINT_SPEED_MM_S, WRITE_UUID

_LOGGER = logging.getLogger(__name__)


try:
    from habluetooth import BaseHaRemoteScanner
except ImportError:  # pragma: no cover
    BaseHaRemoteScanner = None


def _is_local(scanner) -> bool:
    """Lokaler Adapter am HA-Rechner (hciX) statt ESPHome-Proxy?"""
    if BaseHaRemoteScanner is not None:
        return not isinstance(scanner, BaseHaRemoteScanner)
    return str(getattr(scanner, "adapter", "")).startswith("hci")


class _PreferProxyClient(BleakClientWithServiceCache):
    """Verbindet bevorzugt über einen Bluetooth-Proxy.

    Der T02 trennt Verbindungen über den lokalen BlueZ-Adapter nach ~0,3 s,
    HA wählt aber standardmäßig nach Signalstärke. Wir überschreiben die
    Pfadwahl von HAs Bluetooth-Wrapper (habluetooth) und fallen bei
    Problemen auf dessen normale Wahl zurück.
    """

    def __init__(self, address_or_ble_device, *args, **kwargs) -> None:
        super().__init__(address_or_ble_device, *args, **kwargs)
        self._t02_address = getattr(address_or_ble_device, "address", str(address_or_ble_device))

    def _async_get_best_available_backend_and_device(self, manager):
        try:
            devices = [
                d for d in manager.async_scanner_devices_by_address(self._t02_address, True)
                if not _is_local(d.scanner)
            ]
            devices.sort(key=lambda d: d.advertisement.rssi or -127, reverse=True)
            for dev in devices:
                backend = self._async_get_backend_for_ble_device(manager, dev.scanner, dev.ble_device)
                if backend is not None:
                    _LOGGER.debug("Verbinde über %s (RSSI %s)", dev.scanner.name, dev.advertisement.rssi)
                    return backend
            _LOGGER.debug("Kein Proxy mit freiem Platz – nutze Standardwahl")
        except Exception as err:  # noqa: BLE001 – interne API, nie den Druck daran scheitern lassen
            _LOGGER.debug("Proxy-Wahl nicht möglich (%s) – nutze Standardwahl", err)
        return super()._async_get_best_available_backend_and_device(manager)


class T02Printer:
    """Ein T02-Drucker; Druckaufträge werden nacheinander abgearbeitet."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self._lock = asyncio.Lock()
        self.coordinator = None  # StatusCoordinator, gesetzt in async_setup_entry

    async def send(self, job: bytes, image_height_px: int) -> None:
        async with self._lock:
            for attempt in (1, 2, 3):
                try:
                    await self._send_once(job, image_height_px)
                    return
                except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                    if attempt == 3:
                        raise HomeAssistantError(f"Druck an {self.name} fehlgeschlagen: {err}") from err
                    _LOGGER.warning("Druckversuch fehlgeschlagen (%s), neuer Versuch", err)
                    await asyncio.sleep(3)

    async def _send_once(self, job: bytes, image_height_px: int) -> None:
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise HomeAssistantError(
                f"{self.name} ({self.address}) nicht in Reichweite oder ausgeschaltet"
            )
        client = await self._connect(device, max_attempts=3)
        try:
            try:
                _ = client.services
            except BleakError:
                # Abfrage unvollständig (v. a. über ESPHome-Proxy) -> Cache leeren, neu verbinden
                _LOGGER.debug("Dienstliste fehlt, Cache wird geleert")
                await client.clear_cache()
                raise
            char = self._find_write_char(client)
            # Ohne Bestätigung schreiben (wie die Phomemo-App); mit Bestätigung meldet
            # der T02 "GATT Unlikely Error". Pausen zwischen den Paketen gegen Überlauf.
            with_response = "write-without-response" not in char.properties
            size = max(20, min(CHUNK_SIZE, (getattr(client, "mtu_size", 23) or 23) - 3))
            _LOGGER.debug(
                "Sende %d Bytes an %s (%s), Paket %d, mit Bestätigung=%s",
                len(job), char.uuid, ",".join(char.properties), size, with_response,
            )
            for pos in range(0, len(job), size):
                chunk = job[pos:pos + size]
                for retry in range(5):
                    try:
                        await client.write_gatt_char(char, chunk, response=with_response)
                        break
                    except BleakError as err:
                        if "initiate write" not in str(err) or retry == 4:
                            raise
                        await asyncio.sleep(0.1 * (retry + 1))  # Puffer voll -> kurz warten
                if not with_response:
                    await asyncio.sleep(CHUNK_DELAY)
            # Verbindung halten, bis der Drucker fertig ist (8 px = 1 mm)
            await asyncio.sleep(min(90, image_height_px / 8 / PRINT_SPEED_MM_S + 2))
        finally:
            await client.disconnect()

    async def query_status(self) -> dict | None:
        """Liest Akku, Papier, Deckel, Firmware und Seriennummer in einer Verbindung.

        Antworten kommen auf ff03 als ``1a <typ> <daten>``:
        04 Akku in %, 06 Papier (89 = da, 88 = leer), 05 Deckel, 07 Firmware, 08 Seriennummer,
        09 Abschaltzeit.
        Gibt None zurück, wenn der Drucker nicht antwortet oder Statusmeldungen ablehnt.
        Wirft HomeAssistantError, wenn er nicht erreichbar ist.
        """
        async with self._lock:
            device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if device is None:
                raise HomeAssistantError(f"{self.name} nicht in Reichweite oder ausgeschaltet")
            replies: dict[int, bytes] = {}
            got = asyncio.Event()

            def on_notify(_sender, data: bytearray) -> None:
                _LOGGER.debug("%s meldet: %s", self.name, data.hex(" "))
                if len(data) >= 3 and data[0] == 0x1A:
                    replies[data[1]] = bytes(data[2:])
                    got.set()

            client = None
            for attempt in (1, 2):
                client = await self._connect(device, max_attempts=2)
                try:
                    await client.start_notify(NOTIFY_UUID, on_notify)
                    break
                except BleakError as err:
                    # Der T02 gibt Statusmeldungen teils nur über eine gekoppelte
                    # (verschlüsselte) Verbindung heraus: "Insufficient authentication".
                    if "authentication" in str(err).lower() or "encryption" in str(err).lower():
                        if await self._try_pair(client):
                            try:
                                await client.start_notify(NOTIFY_UUID, on_notify)
                                break
                            except BleakError as err2:
                                err = err2
                    await client.disconnect()
                    if attempt == 2:
                        _LOGGER.warning("%s lehnt Statusmeldungen ab (%s)", self.name, err)
                        return None
                    _LOGGER.debug("Statusmeldungen abgelehnt (%s), neuer Versuch in 5 s", err)
                    await asyncio.sleep(5)
            try:
                await asyncio.sleep(1)  # Begrüßungsmeldungen des Druckers abwarten
                char = self._find_write_char(client)
                for code in (0x08, 0x11, 0x12, 0x07, 0x09, 0x0E):
                    expect = {0x08: 0x04, 0x11: 0x06, 0x12: 0x05, 0x07: 0x07, 0x09: 0x08, 0x0E: 0x09}[code]
                    for _attempt in range(2):
                        if expect in replies or not client.is_connected:
                            break
                        got.clear()
                        await client.write_gatt_char(char, bytes([0x1F, 0x11, code]), response=False)
                        try:
                            await asyncio.wait_for(got.wait(), timeout=2)
                            await asyncio.sleep(0.1)
                        except (TimeoutError, asyncio.TimeoutError):
                            pass
            finally:
                await client.disconnect()

        status: dict = {}
        if (b := replies.get(0x04)) and 0 <= b[0] <= 100:
            status["battery"] = b[0]
        if b := replies.get(0x06):
            status["paper_raw"] = b[0]
            status["paper"] = {0x89: True, 0x88: False}.get(b[0])
        if b := replies.get(0x05):
            status["cover_raw"] = b[0]
        if (b := replies.get(0x07)) and len(b) >= 3:
            status["firmware"] = f"{b[0]}.{b[1]}.{b[2]}"
        if b := replies.get(0x09):
            status["auto_off_raw"] = b[0]  # Abschaltzeit (Einheit unbekannt, bei 0x0c ≈ 1–2 h)
        if b := replies.get(0x08):
            status["serial"] = b.decode("ascii", "replace").strip()
        if not status:
            _LOGGER.warning("%s hat auf keine Statusabfrage geantwortet", self.name)
            return None
        _LOGGER.debug("%s Status: %s", self.name, status)
        return status

    async def _try_pair(self, client) -> bool:
        """Koppelt mit dem Drucker (über ESPHome-Proxy oder lokalen Adapter)."""
        try:
            _LOGGER.debug("%s: Kopplung wird versucht", self.name)
            await client.pair()
        except (BleakError, NotImplementedError, TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.debug("%s: Kopplung nicht möglich (%s)", self.name, err)
            return False
        _LOGGER.info("%s: gekoppelt", self.name)
        await asyncio.sleep(0.5)
        return True

    async def _connect(self, device, max_attempts: int = 3):
        """Verbindet (bevorzugt über Proxy); Fehler werden verständlich gemeldet."""
        try:
            return await establish_connection(
                _PreferProxyClient, device, self.name, max_attempts=max_attempts,
                use_services_cache=False,  # Kanäle immer frisch abfragen
                disconnected_callback=self._on_disconnect,
            )
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            rssi = info.rssi if info else None
            hint = ""
            if rssi is not None and rssi < -85:
                hint = f" – Signal sehr schwach (RSSI {rssi}), Drucker näher an den Bluetooth-Proxy stellen"
            raise HomeAssistantError(f"{self.name}: keine Verbindung{hint}") from err

    def _find_write_char(self, client):
        """ff02 bevorzugen; sonst erste beschreibbare Charakteristik (Nachbauten)."""
        writable = []
        for service in client.services:
            for char in service.characteristics:
                _LOGGER.debug("Dienst %s / %s: %s", service.uuid, char.uuid, char.properties)
                if {"write", "write-without-response"} & set(char.properties):
                    writable.append(char)
        for char in writable:
            if char.uuid.lower() == WRITE_UUID:
                return char
        if writable:
            _LOGGER.warning("ff02 nicht gefunden, nutze %s", writable[0].uuid)
            return writable[0]
        raise HomeAssistantError(f"{self.name}: keine beschreibbare Charakteristik gefunden")

    def _on_disconnect(self, _client) -> None:
        _LOGGER.debug("%s hat die Verbindung getrennt", self.name)
