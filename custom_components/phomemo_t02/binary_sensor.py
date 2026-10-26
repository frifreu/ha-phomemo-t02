"""Papierstatus des Phomemo T02 (an = Problem: Papier fehlt)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    async_add_entities([PaperSensor(coordinator)])


class PaperSensor(CoordinatorEntity[StatusCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Papier fehlt"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:paper-roll-outline"

    def __init__(self, coordinator: StatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.printer.address}_paper"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        paper = (self.coordinator.data or {}).get("paper")
        return None if paper is None else not paper

    @property
    def extra_state_attributes(self) -> dict:
        raw = (self.coordinator.data or {}).get("paper_raw")
        return {"rohwert": f"{raw:02x}" if raw is not None else None}
