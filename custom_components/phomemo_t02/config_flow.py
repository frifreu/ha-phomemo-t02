"""Einrichtung per Bluetooth-Erkennung oder manueller MAC-Adresse."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ADDRESS, DOMAIN, SERVICE_UUID

MANUAL = "manual"
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _looks_like_t02(info: BluetoothServiceInfoBleak) -> bool:
    name = (info.name or "").upper()
    return SERVICE_UUID in info.service_uuids or name.startswith(("T02", "MR.IN"))


class T02ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}
        self._discovery: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name or discovery_info.address}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovery is not None
        name = self._discovery.name or "Phomemo T02"
        if user_input is not None:
            return self.async_create_entry(title=name, data={CONF_ADDRESS: self._discovery.address})
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm", description_placeholders={"name": name})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == MANUAL:
                return await self.async_step_manual()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=self._discovered[address], data={CONF_ADDRESS: address})

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address not in current and _looks_like_t02(info):
                self._discovered[info.address] = f"{info.name or 'T02'} ({info.address})"
        options = {**self._discovered, MANUAL: "MAC-Adresse manuell eingeben"}
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)})
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper().replace("-", ":")
            if MAC_RE.match(address):
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"T02 ({address})", data={CONF_ADDRESS: address})
            errors[CONF_ADDRESS] = "invalid_mac"
        return self.async_show_form(
            step_id="manual", data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}), errors=errors
        )
