"""Regelmäßige Statusabfrage (Akku, Papier) des Phomemo T02."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .printer import T02Printer

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(hours=6)


class StatusCoordinator(DataUpdateCoordinator[dict]):
    """Fragt alle 6 Stunden und nach jedem Druck ab.

    Ist der Drucker aus, bleiben die letzten Werte stehen; ``reachable`` wird False.
    """

    def __init__(self, hass: HomeAssistant, printer: T02Printer) -> None:
        super().__init__(hass, _LOGGER, name=f"{printer.name} Status", update_interval=UPDATE_INTERVAL)
        self.printer = printer

    async def _async_update_data(self) -> dict:
        last = dict(self.data or {})
        try:
            status = await self.printer.query_status()
        except HomeAssistantError as err:
            _LOGGER.debug("Statusabfrage übersprungen: %s", err)
            return {**last, "reachable": False}
        if not status:
            return {**last, "reachable": True}
        return {**last, **status, "reachable": True, "updated": dt_util.now()}

    @property
    def device_info(self) -> DeviceInfo:
        data = self.data or {}
        return DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self.printer.address)},
            identifiers={(DOMAIN, self.printer.address)},
            name=self.printer.name,
            manufacturer="Phomemo (kompatibel)",
            model="T02",
            sw_version=data.get("firmware"),
            serial_number=data.get("serial"),
        )
