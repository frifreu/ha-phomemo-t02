"""Geburtstagskalender aus iCloud-Kontakten."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .carddav import Birthday, CardDavAuthError, CardDavError, ICloudBirthdays
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(hours=12)
MARK = "★"


class BirthdayCoordinator(DataUpdateCoordinator[list[Birthday]]):
    """Liest die Kontakte zweimal täglich neu ein."""

    def __init__(self, hass: HomeAssistant, client: ICloudBirthdays) -> None:
        super().__init__(hass, _LOGGER, name="iCloud-Geburtstage", update_interval=UPDATE_INTERVAL)
        self.client = client

    async def _async_update_data(self) -> list[Birthday]:
        try:
            return await self.client.fetch()
        except CardDavAuthError as err:
            raise UpdateFailed(
                "iCloud lehnt die Anmeldung ab – App-spezifisches Passwort prüfen"
            ) from err
        except CardDavError as err:
            raise UpdateFailed(str(err)) from err


def _summary(bday: Birthday, year: int) -> str:
    age = bday.age_in(year)
    return f"{MARK} {bday.name} ({age})" if age and age > 0 else f"{MARK} {bday.name}"


def _events(birthdays: list[Birthday], first: date, last: date) -> list[CalendarEvent]:
    """Alle Geburtstage mit Datum first <= d <= last."""
    events = []
    for year in range(first.year, last.year + 1):
        for bday in birthdays:
            d = bday.on(year)
            if first <= d <= last:
                age = bday.age_in(year)
                events.append(CalendarEvent(
                    start=d, end=d + timedelta(days=1), summary=_summary(bday, year),
                    description=f"{bday.name} wird {age}" if age and age > 0 else f"Geburtstag von {bday.name}",
                ))
    events.sort(key=lambda e: (e.start, e.summary))
    return events


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BirthdayCoordinator = entry.runtime_data
    async_add_entities([BirthdayCalendar(coordinator, entry)])


class BirthdayCalendar(CoordinatorEntity[BirthdayCoordinator], CalendarEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:cake-variant"

    def __init__(self, coordinator: BirthdayCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_birthdays"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"birthdays_{entry.entry_id}")},
            name="Geburtstage",
            manufacturer="Apple iCloud",
            model="Kontakte (CardDAV)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        upcoming = _events(self.coordinator.data or [], today, today + timedelta(days=366))
        return upcoming[0] if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        first = dt_util.as_local(start_date).date()
        # Ganztägige Termine überlappen, solange sie vor dem Ende beginnen
        last_dt = dt_util.as_local(end_date)
        last = last_dt.date() if last_dt.time() != datetime.min.time() else last_dt.date() - timedelta(days=1)
        if last < first:
            return []
        return _events(self.coordinator.data or [], first, last)
