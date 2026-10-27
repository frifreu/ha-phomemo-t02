"""Phomemo T02 Mini-Thermodrucker für Home Assistant."""

from __future__ import annotations

import base64
import binascii
from datetime import date, timedelta
import io
import logging

import aiohttp

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import render
from .const import (
    CONF_ADDRESS,
    DEFAULT_DENSITY,
    DEFAULT_STROKE,
    DOMAIN,
    SERVICE_FEED,
    SERVICE_PRINT_CALENDAR,
    SERVICE_PRINT_IMAGE,
    SERVICE_PRINT_TEXT,
)
from .printer import T02Printer

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_PRINTER = "printer"
BASE = {
    vol.Optional(ATTR_PRINTER): cv.string,
    vol.Optional("density", default=DEFAULT_DENSITY): vol.All(vol.Coerce(int), vol.Range(min=1, max=15)),
}
STROKE = {vol.Optional("stroke", default=DEFAULT_STROKE): vol.All(vol.Coerce(int), vol.Range(min=0, max=3))}

TEXT_SCHEMA = vol.Schema({
    **BASE,
    **STROKE,
    vol.Required("text"): cv.string,
    vol.Optional("font_size", default=22): vol.All(vol.Coerce(int), vol.Range(min=10, max=60)),
})
IMAGE_SCHEMA = vol.Schema({
    **BASE,
    vol.Exclusive("path", "quelle"): cv.string,
    vol.Exclusive("image_base64", "quelle"): cv.string,
    vol.Exclusive("url", "quelle"): cv.url,
    vol.Optional("rotate", default="auto"): vol.In(["auto", "always", "never"]),
    vol.Optional("dither", default="atkinson"): vol.In(["atkinson", "floyd", "none"]),
})
FEED_SCHEMA = vol.Schema({**BASE, vol.Optional("lines", default=40): vol.All(vol.Coerce(int), vol.Range(min=1, max=255))})
CALENDAR_SCHEMA = vol.Schema({
    **BASE,
    **STROKE,
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional("day_offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=-7, max=30)),
    vol.Optional("show_location", default=True): cv.boolean,
    vol.Optional("skip_if_empty", default=False): cv.boolean,
    vol.Optional("preview_entity_id", default=[]): cv.entity_ids,
    vol.Optional("preview_days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=14)),
    vol.Optional("font_size", default=22): vol.All(vol.Coerce(int), vol.Range(min=10, max=60)),
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})

    def _printer(call: ServiceCall) -> T02Printer:
        printers: dict[str, T02Printer] = hass.data[DOMAIN]
        if not printers:
            raise ServiceValidationError("Kein Phomemo T02 eingerichtet")
        wanted = call.data.get(ATTR_PRINTER)
        if wanted is None:
            if len(printers) > 1:
                raise ServiceValidationError("Mehrere Drucker eingerichtet – bitte 'printer' angeben")
            return next(iter(printers.values()))
        for entry_id, prn in printers.items():
            if wanted in (entry_id, prn.address, prn.address.upper(), prn.name):
                return prn
        raise ServiceValidationError(f"Drucker '{wanted}' nicht gefunden")

    async def _print_image(prn: T02Printer, img, density: int) -> None:
        job = await hass.async_add_executor_job(render.build_job, img, 4, density)
        await prn.send(job, img.height)
        if prn.coordinator is not None:  # nach dem Druck Status auffrischen
            # Nicht sofort: direkt nach dem Druck lehnt der T02 Statusabfragen oft ab
            coordinator = prn.coordinator

            @callback
            def _refresh_later(_now) -> None:
                hass.async_create_task(coordinator.async_request_refresh())

            async_call_later(hass, 15, _refresh_later)

    async def print_text(call: ServiceCall) -> None:
        img = await hass.async_add_executor_job(render.render_text, call.data["text"], call.data["font_size"], call.data["stroke"])
        await _print_image(_printer(call), img, call.data["density"])

    async def print_image(call: ServiceCall) -> None:
        """Bild aus Datei (path), als Base64-Text (image_base64, z. B. iPhone-Kurzbefehl) oder per URL."""
        if "path" in call.data:
            source = call.data["path"]
            if not hass.config.is_allowed_path(source):
                raise ServiceValidationError(f"Pfad {source} ist nicht freigegeben (allowlist_external_dirs)")
        elif "image_base64" in call.data:
            try:
                raw = call.data["image_base64"]
                if raw.startswith("data:"):  # data:image/jpeg;base64,...
                    raw = raw.split(",", 1)[1]
                source = io.BytesIO(base64.b64decode(raw))  # Zeilenumbrüche werden ignoriert
            except (ValueError, binascii.Error) as err:
                raise ServiceValidationError(f"image_base64 ist kein gültiges Base64: {err}") from err
        elif "url" in call.data:
            session = async_get_clientsession(hass)
            try:
                async with session.get(call.data["url"], timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    resp.raise_for_status()
                    source = io.BytesIO(await resp.read())
            except (aiohttp.ClientError, TimeoutError) as err:
                raise HomeAssistantError(f"Bild konnte nicht geladen werden: {err}") from err
        else:
            raise ServiceValidationError("Bitte path, image_base64 oder url angeben")
        try:
            img = await hass.async_add_executor_job(
                render.prepare_image, source, call.data["rotate"], call.data["dither"]
            )
        except OSError as err:
            raise HomeAssistantError(f"Bild nicht lesbar: {err}") from err
        await _print_image(_printer(call), img, call.data["density"])

    async def feed(call: ServiceCall) -> None:
        prn = _printer(call)
        await prn.send(b"\x1b\x40\x1b\x64" + bytes([call.data["lines"]]), call.data["lines"] * 4)

    async def print_calendar(call: ServiceCall) -> None:
        prn = _printer(call)
        day = dt_util.now().date() + timedelta(days=call.data["day_offset"])
        entity_ids: list[str] = call.data[ATTR_ENTITY_ID]
        preview_ids: list[str] = call.data.get("preview_entity_id", [])
        preview_days: int = call.data["preview_days"]

        async def fetch(ids: list[str], first: date, days: int) -> dict:
            if not ids:
                return {}
            begin = dt_util.start_of_local_day(first)
            # 1 s vor Mitternacht enden: sonst zählt HA Termine, die um 0:00 beginnen, noch mit
            stop = dt_util.start_of_local_day(first + timedelta(days=days)) - timedelta(seconds=1)
            response = await hass.services.async_call(
                "calendar", "get_events",
                {ATTR_ENTITY_ID: ids, "start_date_time": begin, "end_date_time": stop},
                blocking=True, return_response=True,
            )
            result = {}
            for eid in ids:
                state = hass.states.get(eid)
                name = state.attributes.get("friendly_name", eid) if state else eid
                result[eid] = (name, (response or {}).get(eid, {}).get("events", []))
            return result

        calendars = await fetch(entity_ids, day, 1)
        preview = await fetch(preview_ids, day + timedelta(days=1), preview_days)

        if call.data["skip_if_empty"] and not any(ev for _, ev in [*calendars.values(), *preview.values()]):
            _LOGGER.info("Keine Termine am %s – nichts gedruckt", day)
            return
        text = render.format_calendar(
            day, calendars, call.data["show_location"], tz=dt_util.get_default_time_zone(),
            preview=preview, preview_title="Morgen" if preview_days == 1 else "Demnächst",
        )
        img = await hass.async_add_executor_job(render.render_text, text, call.data["font_size"], call.data["stroke"])
        await _print_image(prn, img, call.data["density"])

    async def density_test(call: ServiceCall) -> None:
        job, height = await hass.async_add_executor_job(render.density_test_job, call.data["levels"])
        await _printer(call).send(job, height)

    hass.services.async_register(DOMAIN, "density_test", density_test, vol.Schema({
        vol.Optional(ATTR_PRINTER): cv.string,
        vol.Optional("levels", default=[1, 2, 3, 4, 5, 6, 8]): vol.All(
            cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=0, max=255))]
        ),
    }))

    async def update_status(call: ServiceCall) -> None:
        prn = _printer(call)
        if prn.coordinator is not None:
            await prn.coordinator.async_refresh()

    hass.services.async_register(DOMAIN, "update_status", update_status, vol.Schema({vol.Optional(ATTR_PRINTER): cv.string}))
    hass.services.async_register(DOMAIN, SERVICE_PRINT_TEXT, print_text, TEXT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PRINT_IMAGE, print_image, IMAGE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_FEED, feed, FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PRINT_CALENDAR, print_calendar, CALENDAR_SCHEMA)
    return True


PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import StatusCoordinator

    printer = T02Printer(hass, entry.data[CONF_ADDRESS], entry.title)
    printer.coordinator = StatusCoordinator(hass, printer)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = printer
    await printer.coordinator.async_refresh()  # Drucker aus -> kein Fehler, Werte leer
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
