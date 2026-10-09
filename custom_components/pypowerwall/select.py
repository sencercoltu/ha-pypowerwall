from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PyPowerwallCoordinator
from .data import PyPowerwallConfigEntry
from .entity import PyPowerwallEntity

_LOGGER = logging.getLogger(__name__)

OPERATION_MODES = ["self_consumption", "backup", "autonomous"]
GRID_EXPORT_MODES = ["battery_ok", "pv_only", "never"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyPowerwallConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.has_control_secret:
        async_add_entities([
            PyPowerwallOperationMode(coordinator, entry.entry_id),
            PyPowerwallGridExport(coordinator, entry.entry_id),
        ])


class PyPowerwallOperationMode(PyPowerwallEntity, SelectEntity):
    """Operation mode control."""

    _attr_options = OPERATION_MODES
    _attr_translation_key = "operation_mode_select"
    _attr_icon = "mdi:cog"

    def __init__(self, coordinator: PyPowerwallCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_operation_mode_select"

    @property
    def current_option(self) -> str | None:
        op = self.coordinator.data.get("operation")
        if op and isinstance(op, dict):
            mode = op.get("real_mode")
            if mode in OPERATION_MODES:
                return mode
        return None

    async def async_select_option(self, option: str) -> None:
        success = await self.coordinator.send_command(
            "/control/mode", option
        )
        if not success:
            raise HomeAssistantError(
                f"Failed to set operation mode to {option}"
            )
        await self.coordinator.async_request_refresh()


class PyPowerwallGridExport(PyPowerwallEntity, SelectEntity):
    """Grid export mode control."""

    _attr_options = GRID_EXPORT_MODES
    _attr_translation_key = "grid_export"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator: PyPowerwallCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_grid_export"

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data.get("control_grid_export")
        if data and isinstance(data, dict):
            mode = data.get("grid_export")
            if mode in GRID_EXPORT_MODES:
                return mode
        # [OLD 2026-09-29] replaced: fall back to /api/operation, where pypowerwall-server
        # reports the export policy (it has no GET /control/grid_export). Delete when happy.
        # return None
        # [/OLD 2026-09-29]
        op = self.coordinator.data.get("operation")
        if isinstance(op, dict) and op.get("grid_export") in GRID_EXPORT_MODES:
            return op["grid_export"]
        return None

    async def async_select_option(self, option: str) -> None:
        success = await self.coordinator.send_command(
            "/control/grid_export", option
        )
        if not success:
            raise HomeAssistantError(
                f"Failed to set grid export mode to {option}"
            )
        await self.coordinator.async_request_refresh()
