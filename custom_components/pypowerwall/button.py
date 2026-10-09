"""Islanding buttons (added 2026-10-10).

pypowerwall-server can take the site off grid / reconnect it over the v1r LAN
transport (POST /control/islanding). Created only for pypowerwall-server with
a control secret, and disabled by default: going off grid is a real switch-over.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PyPowerwallCoordinator
from .data import PyPowerwallConfigEntry
from .entity import PyPowerwallEntity
from .services import send_islanding


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyPowerwallConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.has_control_secret and coordinator.supports_islanding:
        async_add_entities(
            [
                PyPowerwallIslandingButton(coordinator, entry.entry_id, "go_off_grid", "off_grid"),
                PyPowerwallIslandingButton(coordinator, entry.entry_id, "reconnect_grid", "on_grid"),
            ]
        )


class PyPowerwallIslandingButton(PyPowerwallEntity, ButtonEntity):
    """Go off grid / reconnect grid."""

    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PyPowerwallCoordinator, entry_id: str, key: str, action: str) -> None:
        super().__init__(coordinator, entry_id)
        self._action = action
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"

    async def async_press(self) -> None:
        await send_islanding(self.coordinator, self._action)
