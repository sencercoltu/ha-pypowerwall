from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PyPowerwallCoordinator
from .data import PyPowerwallConfigEntry
from .entity import PyPowerwallEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyPowerwallConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    # [OLD 2026-09-29] replaced: pypowerwall-server has no /control/max_backup, so the
    # max backup switch is only created where it can work. Delete this block when happy.
    # if coordinator.has_control_secret:
    #     async_add_entities([
    #         PyPowerwallGridCharging(coordinator, entry.entry_id),
    #         PyPowerwallMaxBackup(coordinator, entry.entry_id),
    #     ])
    # [/OLD 2026-09-29]
    if coordinator.has_control_secret:
        entities: list[SwitchEntity] = [PyPowerwallGridCharging(coordinator, entry.entry_id)]
        if coordinator.supports_max_backup:
            entities.append(PyPowerwallMaxBackup(coordinator, entry.entry_id))
        async_add_entities(entities)


class PyPowerwallGridCharging(PyPowerwallEntity, SwitchEntity):
    """Grid charging control switch."""

    _attr_translation_key = "grid_charging"
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator: PyPowerwallCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_grid_charging"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get("control_grid_charging")
        if data and isinstance(data, dict):
            val = data.get("grid_charging")
            if val is not None:
                return bool(val)
        # [OLD 2026-09-29] replaced: fall back to /api/operation, where pypowerwall-server
        # reports grid charging (it has no GET /control/grid_charging). Delete when happy.
        # return None
        # [/OLD 2026-09-29]
        op = self.coordinator.data.get("operation")
        if isinstance(op, dict) and isinstance(op.get("grid_charging"), bool):
            return op["grid_charging"]
        return None

    async def async_turn_on(self, **kwargs) -> None:
        success = await self.coordinator.send_command(
            "/control/grid_charging", "true"
        )
        if not success:
            raise HomeAssistantError("Failed to enable grid charging")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        success = await self.coordinator.send_command(
            "/control/grid_charging", "false"
        )
        if not success:
            raise HomeAssistantError("Failed to disable grid charging")
        await self.coordinator.async_request_refresh()


class PyPowerwallMaxBackup(PyPowerwallEntity, SwitchEntity):
    """Max backup control switch."""

    _attr_translation_key = "max_backup"
    _attr_icon = "mdi:battery-arrow-up"

    def __init__(self, coordinator: PyPowerwallCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_max_backup"

    @property
    def is_on(self) -> bool | None:
        # 'manual_backup' can linger after expiry -> use the active flag, not presence
        return self.coordinator.max_backup_active

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data.get("control_max_backup")
        mb = data.get("manual_backup") if isinstance(data, dict) else None
        if not mb:
            return None
        return {
            "start_time": mb.get("start_time"),
            "end_time": mb.get("end_time"),
            "duration_seconds": mb.get("duration_seconds"),
        }

    async def async_turn_on(self, **kwargs) -> None:
        # Must cancel any existing (possibly lingering) event before scheduling a new one
        data = self.coordinator.data.get("control_max_backup")
        lingering = isinstance(data, dict) and data.get("manual_backup") is not None
        if lingering:
            cancel_ok = await self.coordinator.send_command(
                "/control/max_backup", "cancel"
            )
            if not cancel_ok:
                raise HomeAssistantError("Failed to cancel existing max backup before re-set")
        success = await self.coordinator.send_command(
            "/control/max_backup", self.coordinator.max_backup_duration
        )
        if not success:
            raise HomeAssistantError("Failed to enable max backup")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        success = await self.coordinator.send_command(
            "/control/max_backup", "cancel"
        )
        if not success:
            raise HomeAssistantError("Failed to cancel max backup")
        await self.coordinator.async_request_refresh()
