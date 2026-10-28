"""Akkustand des Phomemo T02 als Sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    async_add_entities([BatterySensor(coordinator)])


class BatterySensor(CoordinatorEntity[StatusCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Akku"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: StatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.printer.address}_battery"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("battery")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "erreichbar": data.get("reachable"),
            "zuletzt_gelesen": data.get("updated"),
            "deckel_rohwert": f"{data['cover_raw']:02x}" if "cover_raw" in data else None,
            "abschaltzeit_rohwert": data.get("auto_off_raw"),
        }
