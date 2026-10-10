"""Sensor entity descriptions and value helpers for PyPowerwall.

Kept separate from sensor.py (entity classes + platform setup) so each file
stays readable; nothing here touches Home Assistant runtime objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,  # 2026-10-10: fan sensors
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,  # 2026-10-10: L3 / remote meter reactive power
    UnitOfTemperature,  # 2026-10-10: Powerwall temperature sensors
    UnitOfTime,
)
from homeassistant.util import dt as dt_util

from .payload import (  # 2026-10-10
    health_version,
    soe_app,
    soe_raw,
    system_status_value,
    troubleshooting_problems,
    ws_to_wh,
)


# ---------------------------------------------------------------------------
#  Description types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class PyPowerwallSensorDescription(SensorEntityDescription):
    """Static sensor — value_fn receives full coordinator data dict."""

    value_fn: Any


@dataclass(frozen=True, kw_only=True)
class VitalsSensorDescription(SensorEntityDescription):
    """Vitals device sensor — value_fn receives the single device dict."""

    value_fn: Any


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------
def _grid_frequency(d: dict) -> float | None:
    """Grid-side frequency.

    Preference: system_status.f_out (site level; null on some firmwares) ->
    TESYNC ISLAND_FreqL1_Main (grid side of the island controller) ->
    per-Powerwall f_out from battery_blocks -> PVAC_Fout (solar inverter
    output, only equal to the grid while on-grid; last resort).
    """
    ss = d.get("system_status") or {}
    if ss.get("f_out"):
        return ss["f_out"]
    vitals = d.get("vitals") or {}
    for key, val in vitals.items():
        if key.startswith("TESYNC") and isinstance(val, dict) and val.get("ISLAND_FreqL1_Main"):
            return val["ISLAND_FreqL1_Main"]
    block_f = [b.get("f_out") for b in ss.get("battery_blocks") or [] if b.get("f_out")]
    if block_f:
        return round(sum(block_f) / len(block_f), 3)
    for key, val in vitals.items():
        if key.startswith("PVAC") and isinstance(val, dict):
            return val.get("PVAC_Fout")
    return None


# [OLD 2026-10-10] replaced: /api/meters/site was preferred over /aggregates, but on
# TEDAPI / v1r transports pypowerwall answers /api/meters/site with hard-coded MOCK data
# (energy_imported 6995047.55 Wh, energy_exported 4507438.17 Wh, timestamp 2023-12-16),
# so Grid Energy Imported/Exported were stuck at those numbers. Delete this block when happy.
# def _site_meter_energy(d: dict, field: str) -> float | None:
#     """Sum a lifetime energy field over the site meters in /api/meters/site.
#
#     The endpoint returns a list of meters (location 'site'), each with
#     Cached_readings.energy_imported/exported in Wh.
#     """
#     meters = d.get("meters_site")
#     if not isinstance(meters, list):
#         return None
#     total = 0.0
#     seen = False
#     for m in meters:
#         if not isinstance(m, dict) or m.get("location") not in (None, "site"):
#             continue
#         v = (m.get("Cached_readings") or {}).get(field)
#         if v:
#             total += float(v)
#             seen = True
#     return total if seen else None
#
#
# def _energy(section: str, field: str):
#     """Lifetime energy counter (Wh) for a meter section.
#
#     /api/meters/site is authoritative for the site meter; /aggregates carries
#     the same fields for site/battery/load/solar but is zeroed on some proxy
#     transports (tedapi/v1r), so a 0 there is treated as 'unknown' and the
#     entity keeps its last value instead of resetting a TOTAL_INCREASING sensor.
#     """
#
#     def _fn(d: dict) -> float | None:
#         if section == "site":
#             v = _site_meter_energy(d, field)
#             if v:
#                 return v
#         v = (d.get("aggregates") or {}).get(section, {}).get(field)
#         return v if v else None
#
#     return _fn
# [/OLD 2026-10-10]
# pypowerwall's TEDAPI mock for /api/meters/site (pypowerwall/tedapi/mock_data.py METERS_SITE)
MOCK_SITE_METER_SERIAL = "JBL12345Y1F012synchrometerX"
MOCK_SITE_METER_TIME = "2023-12-16T11:48:34"


def _is_mock_site_meter(meter: dict) -> bool:
    """True for pypowerwall's canned /api/meters/site answer (TEDAPI / cloud / FleetAPI modes)."""
    serial = (meter.get("connection") or {}).get("device_serial")
    seen = str((meter.get("Cached_readings") or {}).get("last_communication_time") or "")
    return serial == MOCK_SITE_METER_SERIAL or seen.startswith(MOCK_SITE_METER_TIME)


def _site_meter_energy(d: dict, field: str) -> float | None:
    """Sum a lifetime energy field over the *real* site meters in /api/meters/site.

    The endpoint returns a list of meters (location 'site'), each with
    Cached_readings.energy_imported/exported in Wh. pypowerwall's mock answer
    (served on TEDAPI / v1r) is ignored.
    """
    meters = d.get("meters_site")
    if not isinstance(meters, list):
        return None
    total = 0.0
    seen = False
    for m in meters:
        if not isinstance(m, dict) or m.get("location") not in (None, "site"):
            continue
        if _is_mock_site_meter(m):
            continue
        v = (m.get("Cached_readings") or {}).get(field)
        if v:
            total += float(v)
            seen = True
    return total if seen else None


# Fallback for the grid counters: the gateway's own meter in vitals
# (TEMSA = Backup Switch meter Z, TESYNC = Backup Gateway 2 site meter X).
_VITALS_SITE_ENERGY = {
    "energy_imported": (("TEMSA", "METER_Z_LifetimeEnergyImport"), ("TESYNC", "METER_X_LifetimeEnergyImport")),
    "energy_exported": (("TEMSA", "METER_Z_LifetimeEnergyExport"), ("TESYNC", "METER_X_LifetimeEnergyExport")),
}


def _vitals_site_energy(d: dict, field: str) -> float | None:
    for prefix, key in _VITALS_SITE_ENERGY.get(field, ()):
        for vkey, block in (d.get("vitals") or {}).items():
            if vkey.startswith(prefix) and isinstance(block, dict) and block.get(key):
                return float(block[key])
    return None


def _energy(section: str, field: str):
    """Lifetime energy counter (Wh) for a meter section.

    Order: /aggregates (pypowerwall >= 0.16.5 fills it from the gateway on
    TEDAPI too) -> a real (non-mock) /api/meters/site -> for the grid, the
    gateway meter's lifetime counter in vitals. A 0 / missing value is
    'unknown', so a TOTAL_INCREASING sensor keeps its last value instead of
    resetting.
    """

    def _fn(d: dict) -> float | None:
        v = (d.get("aggregates") or {}).get(section, {}).get(field)
        if v:
            return v
        if section == "site":
            return _site_meter_energy(d, field) or _vitals_site_energy(d, field)
        return None

    return _fn


def _grid_export_policy(d: dict) -> str | None:
    """Export policy: proxy GET /control/grid_export, else /api/operation (server). 2026-10-10."""
    ctl = d.get("control_grid_export")
    if isinstance(ctl, dict) and ctl.get("grid_export") in ("battery_ok", "pv_only", "never"):
        return ctl["grid_export"]
    op = d.get("operation")
    if isinstance(op, dict) and op.get("grid_export") in ("battery_ok", "pv_only", "never"):
        return op["grid_export"]
    return None


def _sum_phases(meter: str, field: str):
    """Sum of a per-CT value over phases A-C of a TESYNC meter (None when no phase reports). 2026-10-10."""

    def _fn(v: dict) -> float | None:
        vals = [v.get(f"METER_{meter}_CT{ph}_{field}") for ph in "ABC"]
        vals = [x for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
        return round(sum(vals), 3) if vals else None

    return _fn


def _parse_ts(value):
    """Parse an ISO timestamp from the proxy into an aware datetime (or None)."""
    if not value:
        return None
    dt = dt_util.parse_datetime(str(value))
    if dt is None:
        return None
    # The proxy's own timestamps (health.startup_time) are naive local time.
    return dt if dt.tzinfo else dt_util.as_utc(dt.replace(tzinfo=dt_util.get_default_time_zone()))


# Energy sensors are only created when the proxy actually reports the counter
# (some transports zero /aggregates energy fields; then only /api/meters/site works).
ENERGY_SENSOR_KEYS = frozenset(
    {
        "grid_energy_imported",
        "grid_energy_exported",
        "solar_energy_produced",
        "battery_energy_charged",
        "battery_energy_discharged",
        "home_energy_consumed",
    }
)


NOMINAL_PACK_WH = 13500  # Powerwall 2 / + / 3 and their expansion packs are all 13.5 kWh nominal


def observed_pack_count(d: dict) -> int:
    """Number of battery packs (Powerwalls + expansions) visible in this poll.

    Counts TEPOD devices in vitals (one per pack), then available_blocks, then
    the battery_blocks list. When a proxy transport is degraded the gateway
    only reports a subset of the packs, so this can be *lower* than reality;
    the coordinator keeps the maximum ever seen as data["pack_count"].
    """
    vitals = d.get("vitals") or {}
    tepods = sum(1 for k in vitals if k.startswith("TEPOD"))
    ss = d.get("system_status") or {}
    return max(tepods, ss.get("available_blocks") or 0, len(ss.get("battery_blocks") or []))


def _pack_count(d: dict) -> int:
    """Sticky pack count (max ever observed), falling back to this poll's count."""
    return int(d.get("pack_count") or observed_pack_count(d))


def _capacity_pct(d: dict) -> float | None:
    """Full-pack energy as % of the system's nominal capacity (13.5 kWh per pack).

    New packs read slightly above 100 %; the value trends down with age.
    """
    ss = d.get("system_status") or {}
    full = ss.get("nominal_full_pack_energy")
    packs = _pack_count(d)
    if not full or not packs:
        return None
    return round(full / (NOMINAL_PACK_WH * packs) * 100, 1)


# ---------------------------------------------------------------------------
#  Main device sensors
# ---------------------------------------------------------------------------
MAIN_SENSORS: tuple[PyPowerwallSensorDescription, ...] = (
    # Power
    PyPowerwallSensorDescription(
        key="solar_power",
        translation_key="solar_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:solar-power",
        value_fn=lambda d: d["json"].get("solar"),
    ),
    PyPowerwallSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-charging",
        value_fn=lambda d: d["json"].get("battery"),
    ),
    PyPowerwallSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:transmission-tower",
        value_fn=lambda d: d["json"].get("grid"),
    ),
    PyPowerwallSensorDescription(
        key="home_power",
        translation_key="home_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda d: d["json"].get("home"),
    ),
    # Battery
    PyPowerwallSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        # [OLD 2026-10-10] replaced: /json.soe is the raw level on the proxy but the
        # app-scaled level on pypowerwall-server (raw is soe_raw there), so the same
        # sensor meant two different things. Now always raw. Delete when happy.
        # value_fn=lambda d: round(d["json"]["soe"], 1)
        # if d["json"].get("soe") is not None
        # else None,
        # [/OLD 2026-10-10]
        value_fn=lambda d: round(soe_raw(d["json"]), 1) if soe_raw(d["json"]) is not None else None,
    ),
    # 2026-10-10: the level as the Tesla app shows it (pypowerwall-server "battery")
    PyPowerwallSensorDescription(
        key="battery_level_app",
        translation_key="battery_level_app",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: round(soe_app(d["json"]), 1) if soe_app(d["json"]) is not None else None,
    ),
    PyPowerwallSensorDescription(
        key="battery_reserve",
        translation_key="battery_reserve",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:battery-lock",
        # Same source as the backup_reserve number entity: /api/operation is the
        # raw setting; /json.reserve is pypowerwall's *scaled* value (it accounts
        # for the hidden ~5 % floor) and can differ by a few percent.
        value_fn=lambda d: (
            (d.get("operation") or {}).get("backup_reserve_percent")
            if (d.get("operation") or {}).get("backup_reserve_percent") is not None
            else d["json"].get("reserve")
        ),
    ),
    PyPowerwallSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:timer-outline",
        value_fn=lambda d: round(d["json"]["time_remaining_hours"], 2)
        if d["json"].get("time_remaining_hours") is not None
        else None,
    ),
    # Grid
    PyPowerwallSensorDescription(
        key="grid_voltage",
        translation_key="grid_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["aggregates"]
        .get("site", {})
        .get("instant_average_voltage"),
    ),
    PyPowerwallSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_grid_frequency,
    ),
    # Alerts
    PyPowerwallSensorDescription(
        key="alert_count",
        translation_key="alert_count",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: sum(
            len(v.get("alerts", []))
            for v in (d.get("vitals") or {}).values()
            if isinstance(v, dict)
        ),
    ),
    PyPowerwallSensorDescription(
        key="active_alerts",
        translation_key="active_alerts",
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: ", ".join(sorted({
            alert
            for v in (d.get("vitals") or {}).values()
            if isinstance(v, dict)
            for alert in v.get("alerts", [])
        })) or "None",
    ),
    # Troubleshooting
    PyPowerwallSensorDescription(
        key="troubleshooting_problems",
        translation_key="troubleshooting_problems",
        icon="mdi:wrench",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # [OLD 2026-10-10] replaced: the endpoint returns {"problems": [...]}, so len()
        # of that dict was always 1; and on TEDAPI / cloud / pypowerwall-server the
        # payload is canned anyway. Delete when happy.
        # value_fn=lambda d: len(d.get("troubleshooting") or [])
        # if d.get("troubleshooting") is not None
        # else None,
        # [/OLD 2026-10-10]
        value_fn=lambda d: len(troubleshooting_problems(d)) if troubleshooting_problems(d) is not None else None,
    ),
    # Diagnostics
    PyPowerwallSensorDescription(
        key="operation_mode",
        translation_key="operation_mode",
        icon="mdi:cog",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("operation") or {}).get("real_mode"),
    ),
    # 2026-10-10: read-only export policy (pypowerwall-server "grid_export" sensor);
    # works without a control secret when /api/operation carries it (server).
    PyPowerwallSensorDescription(
        key="grid_export_policy",
        translation_key="grid_export_policy",
        icon="mdi:transmission-tower-export",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _grid_export_policy(d),
    ),
    PyPowerwallSensorDescription(
        key="pypowerwall_version",
        translation_key="pypowerwall_version",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        # [OLD 2026-10-10] replaced: pypowerwall-server reports "version", not
        # "pypowerwall", in /health -> the sensor was always unknown there. Delete when happy.
        # value_fn=lambda d: (d.get("health") or {}).get("pypowerwall"),
        # [/OLD 2026-10-10]
        value_fn=lambda d: health_version(d.get("health")),
    ),
    PyPowerwallSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("version_info") or {}).get("version"),
    ),
    # Energy (lifetime counters -> Energy Dashboard)
    PyPowerwallSensorDescription(
        key="grid_energy_imported",
        translation_key="grid_energy_imported",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-import",
        value_fn=_energy("site", "energy_imported"),
    ),
    PyPowerwallSensorDescription(
        key="grid_energy_exported",
        translation_key="grid_energy_exported",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-export",
        value_fn=_energy("site", "energy_exported"),
    ),
    PyPowerwallSensorDescription(
        key="solar_energy_produced",
        translation_key="solar_energy_produced",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:solar-power-variant",
        value_fn=_energy("solar", "energy_exported"),
    ),
    PyPowerwallSensorDescription(
        key="battery_energy_charged",
        translation_key="battery_energy_charged",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-up",
        value_fn=_energy("battery", "energy_imported"),
    ),
    PyPowerwallSensorDescription(
        key="battery_energy_discharged",
        translation_key="battery_energy_discharged",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=_energy("battery", "energy_exported"),
    ),
    PyPowerwallSensorDescription(
        key="home_energy_consumed",
        translation_key="home_energy_consumed",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:home-lightning-bolt-outline",
        value_fn=_energy("load", "energy_imported"),
    ),
    # Site battery capacity
    PyPowerwallSensorDescription(
        key="site_full_pack_energy",
        translation_key="site_full_pack_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery",
        value_fn=lambda d: (d.get("system_status") or {}).get("nominal_full_pack_energy"),
    ),
    PyPowerwallSensorDescription(
        key="site_energy_remaining",
        translation_key="site_energy_remaining",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-70",
        value_fn=lambda d: (d.get("system_status") or {}).get("nominal_energy_remaining"),
    ),
    PyPowerwallSensorDescription(
        key="battery_capacity_health",
        translation_key="battery_capacity_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:battery-heart-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_capacity_pct,
    ),
    PyPowerwallSensorDescription(
        key="available_blocks",
        translation_key="available_blocks",
        icon="mdi:battery-multiple",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("system_status") or {}).get("available_blocks"),
    ),
    PyPowerwallSensorDescription(
        key="island_state",
        translation_key="island_state",
        icon="mdi:transmission-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("system_status") or {}).get("system_island_state")
        or (d.get("grid_status") or {}).get("grid_status"),
    ),
    # Gateway
    PyPowerwallSensorDescription(
        key="gateway_uptime",
        translation_key="gateway_uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # [OLD 2026-10-10] replaced: on TEDAPI pypowerwall fills /api/status start_time
        # with the battery *commissioning date* (up_time_seconds is null there), so this
        # showed the install date as the gateway start time. Delete when happy.
        # value_fn=lambda d: _parse_ts((d.get("gateway_status") or {}).get("start_time")),
        # [/OLD 2026-10-10]
        value_fn=lambda d: _parse_ts((d.get("gateway_status") or {}).get("start_time"))
        if (d.get("gateway_status") or {}).get("up_time_seconds") is not None
        else None,
    ),
    # Battery power envelope (system_status). Several of these are null on
    # v1r/older firmwares, so they are disabled by default; enable if populated.
    PyPowerwallSensorDescription(
        key="battery_target_power",
        translation_key="battery_target_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-sync",
        entity_category=EntityCategory.DIAGNOSTIC,
        # [OLD 2026-10-10] replaced: constant 0 from pypowerwall's TEDAPI system_status stub. Delete when happy.
        # value_fn=lambda d: (d.get("system_status") or {}).get("battery_target_power"),
        # [/OLD 2026-10-10]
        value_fn=lambda d: system_status_value(d, "battery_target_power"),
    ),
    PyPowerwallSensorDescription(
        key="max_charge_power",
        translation_key="max_charge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-up-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (d.get("system_status") or {}).get("max_charge_power"),
    ),
    PyPowerwallSensorDescription(
        key="max_discharge_power",
        translation_key="max_discharge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-down-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (d.get("system_status") or {}).get("max_discharge_power"),
    ),
    PyPowerwallSensorDescription(
        key="instantaneous_max_charge_power",
        translation_key="instantaneous_max_charge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-up",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # [OLD 2026-10-10] replaced: constant 0 from pypowerwall's TEDAPI system_status stub. Delete when happy.
        # value_fn=lambda d: (d.get("system_status") or {}).get("instantaneous_max_charge_power"),
        # [/OLD 2026-10-10]
        value_fn=lambda d: system_status_value(d, "instantaneous_max_charge_power"),
    ),
    PyPowerwallSensorDescription(
        key="instantaneous_max_discharge_power",
        translation_key="instantaneous_max_discharge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-down",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # [OLD 2026-10-10] replaced: constant 0 from pypowerwall's TEDAPI system_status stub. Delete when happy.
        # value_fn=lambda d: (d.get("system_status") or {}).get("instantaneous_max_discharge_power"),
        # [/OLD 2026-10-10]
        value_fn=lambda d: system_status_value(d, "instantaneous_max_discharge_power"),
    ),
    PyPowerwallSensorDescription(
        key="inverter_nominal_usable_power",
        translation_key="inverter_nominal_usable_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:sine-wave",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # [OLD 2026-10-10] replaced: constant 0 from pypowerwall's TEDAPI system_status stub. Delete when happy.
        # value_fn=lambda d: (d.get("system_status") or {}).get("inverter_nominal_usable_power"),
        # [/OLD 2026-10-10]
        value_fn=lambda d: system_status_value(d, "inverter_nominal_usable_power"),
    ),
    PyPowerwallSensorDescription(
        key="solar_real_power_limit",
        translation_key="solar_real_power_limit",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:solar-power-variant-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (d.get("system_status") or {}).get("solar_real_power_limit"),
    ),
    # Proxy health
    PyPowerwallSensorDescription(
        key="proxy_uptime",
        translation_key="proxy_uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:server-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: _parse_ts((d.get("health") or {}).get("startup_time")),
    ),
    PyPowerwallSensorDescription(
        key="proxy_data_age",
        translation_key="proxy_data_age",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:timer-sand",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: ((d.get("health") or {}).get("connection_health") or {}).get("last_success_age_seconds"),
    ),
)


# ---------------------------------------------------------------------------
#  Battery pod sensors (per TEPOD device)
# ---------------------------------------------------------------------------
POD_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="pod_soc",
        translation_key="pod_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        # [OLD 2026-10-10] replaced: a missing/None energy value reported 0 % (or raised
        # TypeError on None) instead of unknown during degraded polls. Delete when happy.
        # value_fn=lambda d: round(
        #     d.get("POD_nom_energy_remaining", 0)
        #     / max(d.get("POD_nom_full_pack_energy", 1), 1)
        #     * 100,
        #     1,
        # ),
        # [/OLD 2026-10-10]
        value_fn=lambda d: round(d["POD_nom_energy_remaining"] / d["POD_nom_full_pack_energy"] * 100, 1)
        if d.get("POD_nom_energy_remaining") is not None and d.get("POD_nom_full_pack_energy")
        else None,
    ),
    VitalsSensorDescription(
        key="pod_energy_remaining",
        translation_key="pod_energy_remaining",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-outline",
        value_fn=lambda d: d.get("POD_nom_energy_remaining"),
    ),
    VitalsSensorDescription(
        key="pod_energy_to_charge",
        translation_key="pod_energy_to_charge",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-charging-outline",
        value_fn=lambda d: d.get("POD_nom_energy_to_be_charged"),
    ),
    VitalsSensorDescription(
        key="pod_full_energy",
        translation_key="pod_full_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("POD_nom_full_pack_energy"),
    ),
)


# ---------------------------------------------------------------------------
#  Inverter sensors (per TEPINV device)
# ---------------------------------------------------------------------------
INVERTER_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="inverter_power",
        translation_key="inverter_power",
        # [OLD 2026-10-10] replaced: PINV_Pout is reported in kW (pypowerwall divides
        # by 1000), so -0.97 kW showed as "-0.97 W". Delete when happy.
        # native_unit_of_measurement=UnitOfPower.WATT,
        # suggested_display_precision=0,
        # [/OLD 2026-10-10]
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("PINV_Pout"),
    ),
    VitalsSensorDescription(
        key="inverter_voltage",
        translation_key="inverter_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("PINV_Vout"),
    ),
    VitalsSensorDescription(
        key="inverter_frequency",
        translation_key="inverter_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.get("PINV_Fout"),
    ),
    VitalsSensorDescription(
        key="inverter_state",
        translation_key="inverter_state",
        icon="mdi:state-machine",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("PINV_State"),
    ),
    VitalsSensorDescription(
        key="inverter_vsplit1",
        translation_key="inverter_vsplit1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PINV_VSplit1"),
    ),
    VitalsSensorDescription(
        key="inverter_vsplit2",
        translation_key="inverter_vsplit2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PINV_VSplit2"),
    ),
)


# ---------------------------------------------------------------------------
#  PVAC output sensors (on primary Powerwall device)
# ---------------------------------------------------------------------------
PVAC_OUTPUT_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="pvac_output_power",
        translation_key="pvac_output_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("PVAC_Pout"),
    ),
    VitalsSensorDescription(
        key="pvac_output_voltage",
        translation_key="pvac_output_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PVAC_Vout"),
    ),
    VitalsSensorDescription(
        key="pvac_vl1_ground",
        translation_key="pvac_vl1_ground",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PVAC_VL1Ground"),
    ),
    VitalsSensorDescription(
        key="pvac_vl2_ground",
        translation_key="pvac_vl2_ground",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PVAC_VL2Ground"),
    ),
    VitalsSensorDescription(
        key="pvac_state",
        translation_key="pvac_state",
        icon="mdi:state-machine",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("PVAC_State"),
    ),
)


# ---------------------------------------------------------------------------
#  Grid meter sensors (per TEMSA device)
# ---------------------------------------------------------------------------
GRID_METER_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="grid_l1_power",
        translation_key="grid_l1_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("METER_Z_CTA_InstRealPower"),
    ),
    VitalsSensorDescription(
        key="grid_l2_power",
        translation_key="grid_l2_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("METER_Z_CTB_InstRealPower"),
    ),
    VitalsSensorDescription(
        key="grid_l1_voltage",
        translation_key="grid_l1_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("METER_Z_VL1N"),
    ),
    VitalsSensorDescription(
        key="grid_l2_voltage",
        translation_key="grid_l2_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.get("METER_Z_VL2N"),
    ),
    VitalsSensorDescription(
        key="grid_l1_current",
        translation_key="grid_l1_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTA_I"),
    ),
    VitalsSensorDescription(
        key="grid_l2_current",
        translation_key="grid_l2_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTB_I"),
    ),
    VitalsSensorDescription(
        key="grid_l1_reactive_power",
        translation_key="grid_l1_reactive_power",
        native_unit_of_measurement="var",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTA_InstReactivePower"),
    ),
    VitalsSensorDescription(
        key="grid_l2_reactive_power",
        translation_key="grid_l2_reactive_power",
        native_unit_of_measurement="var",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTB_InstReactivePower"),
    ),
    # 2026-10-10: third phase (three-phase sites); disabled by default
    VitalsSensorDescription(
        key="grid_l3_power",
        translation_key="grid_l3_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTC_InstRealPower"),
    ),
    VitalsSensorDescription(
        key="grid_l3_voltage",
        translation_key="grid_l3_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_VL3N"),
    ),
    VitalsSensorDescription(
        key="grid_l3_current",
        translation_key="grid_l3_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTC_I"),
    ),
    VitalsSensorDescription(
        key="grid_l3_reactive_power",
        translation_key="grid_l3_reactive_power",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_CTC_InstReactivePower"),
    ),
    VitalsSensorDescription(
        key="grid_lifetime_energy_export",
        translation_key="grid_lifetime_energy_export",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_LifetimeEnergyExport"),
    ),
    VitalsSensorDescription(
        key="grid_lifetime_energy_import",
        translation_key="grid_lifetime_energy_import",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("METER_Z_LifetimeEnergyImport"),
    ),
)


# ---------------------------------------------------------------------------
#  Island controller sensors (TESYNC device)
# ---------------------------------------------------------------------------
ISLAND_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="island_freq_l1_main",
        translation_key="island_freq_l1_main",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL1_Main"),
    ),
    VitalsSensorDescription(
        key="island_freq_l2_main",
        translation_key="island_freq_l2_main",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL2_Main"),
    ),
    VitalsSensorDescription(
        key="island_freq_l1_load",
        translation_key="island_freq_l1_load",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL1_Load"),
    ),
    VitalsSensorDescription(
        key="island_freq_l2_load",
        translation_key="island_freq_l2_load",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL2_Load"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l1_main",
        translation_key="island_voltage_l1_main",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL1N_Main"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l2_main",
        translation_key="island_voltage_l2_main",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL2N_Main"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l1_load",
        translation_key="island_voltage_l1_load",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL1N_Load"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l2_load",
        translation_key="island_voltage_l2_load",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL2N_Load"),
    ),
    # 2026-10-10: third phase + the gateway's internal meters X / Y; disabled by default
    VitalsSensorDescription(
        key="island_freq_l3_main",
        translation_key="island_freq_l3_main",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL3_Main"),
    ),
    VitalsSensorDescription(
        key="island_freq_l3_load",
        translation_key="island_freq_l3_load",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_FreqL3_Load"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l3_main",
        translation_key="island_voltage_l3_main",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL3N_Main"),
    ),
    VitalsSensorDescription(
        key="island_voltage_l3_load",
        translation_key="island_voltage_l3_load",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.get("ISLAND_VL3N_Load"),
    ),
    *(
        desc
        for meter in ("X", "Y")
        for desc in (
            VitalsSensorDescription(
                key=f"meter_{meter.lower()}_power",
                translation_key=f"meter_{meter.lower()}_power",
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
                entity_registry_enabled_default=False,
                value_fn=_sum_phases(meter, "InstRealPower"),
            ),
            VitalsSensorDescription(
                key=f"meter_{meter.lower()}_energy_import",
                translation_key=f"meter_{meter.lower()}_energy_import",
                native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL_INCREASING,
                suggested_display_precision=0,
                entity_registry_enabled_default=False,
                value_fn=lambda d, m=meter: d.get(f"METER_{m}_LifetimeEnergyImport") or None,
            ),
            VitalsSensorDescription(
                key=f"meter_{meter.lower()}_energy_export",
                translation_key=f"meter_{meter.lower()}_energy_export",
                native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
                device_class=SensorDeviceClass.ENERGY,
                state_class=SensorStateClass.TOTAL_INCREASING,
                suggested_display_precision=0,
                entity_registry_enabled_default=False,
                value_fn=lambda d, m=meter: d.get(f"METER_{m}_LifetimeEnergyExport") or None,
            ),
        )
    ),
    VitalsSensorDescription(
        key="island_grid_state",
        translation_key="island_grid_state",
        icon="mdi:transmission-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("ISLAND_GridState"),
    ),
)


# ---------------------------------------------------------------------------
#  PV string field definitions (A–F)
# ---------------------------------------------------------------------------
STRING_FIELDS = (
    (
        "power",
        "Power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        "mdi:solar-power",
        0,
    ),
    (
        "voltage",
        "Voltage",
        UnitOfElectricPotential.VOLT,
        SensorDeviceClass.VOLTAGE,
        None,
        1,
    ),
    (
        "current",
        "Current",
        UnitOfElectricCurrent.AMPERE,
        SensorDeviceClass.CURRENT,
        None,
        2,
    ),
)




# ---------------------------------------------------------------------------
#  Per-pack sensors (2026-10-10) — value_fn receives data["packs"][serial],
#  i.e. system_status.battery_blocks merged with /pod (see payload.build_packs).
#  Created enabled only when the pack reports the value at setup (most are
#  null on TEDAPI / v1r transports).
# ---------------------------------------------------------------------------
PACK_SENSORS: tuple[VitalsSensorDescription, ...] = (
    VitalsSensorDescription(
        key="pack_available_charge_power",
        translation_key="pack_available_charge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-up-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("POD_available_charge_power"),
    ),
    VitalsSensorDescription(
        key="pack_available_discharge_power",
        translation_key="pack_available_discharge_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:battery-arrow-down-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("POD_available_dischg_power"),
    ),
    VitalsSensorDescription(
        key="pack_energy_charged",
        translation_key="pack_energy_charged",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-up",
        # 0 means "not reported" (TEDAPI); never reset a total_increasing sensor with it
        value_fn=lambda p: p.get("energy_charged") or None,
    ),
    VitalsSensorDescription(
        key="pack_energy_discharged",
        translation_key="pack_energy_discharged",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=lambda p: p.get("energy_discharged") or None,
    ),
    VitalsSensorDescription(
        key="pack_output_voltage",
        translation_key="pack_output_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,  # duplicates the inverter sensors on most systems
        value_fn=lambda p: p.get("v_out"),
    ),
    VitalsSensorDescription(
        key="pack_output_current",
        translation_key="pack_output_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("i_out"),
    ),
    VitalsSensorDescription(
        key="pack_output_frequency",
        translation_key="pack_output_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,  # duplicates the inverter sensors on most systems
        value_fn=lambda p: p.get("f_out"),
    ),
    VitalsSensorDescription(
        key="pack_grid_state",
        translation_key="pack_grid_state",
        icon="mdi:transmission-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("pinv_grid_state"),
    ),
    VitalsSensorDescription(
        key="pack_op_seq_state",
        translation_key="pack_op_seq_state",
        icon="mdi:state-machine",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("OpSeqState"),
    ),
    VitalsSensorDescription(
        key="pack_firmware_version",
        translation_key="pack_firmware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.get("version"),
    ),
)


# ---------------------------------------------------------------------------
#  Per-unit temperature / fan sensors (2026-10-10) — same metric ids as
#  pypowerwall-server's MQTT discovery; value from data["unit_signals"][serial].
# ---------------------------------------------------------------------------
def _temp(key: str, icon: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon=icon,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


def _fan(key: str, unit: str, icon: str, precision: int) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        icon=icon,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


UNIT_SIGNAL_SENSORS: dict[str, SensorEntityDescription] = {
    "pack_temp_max": _temp("pack_temp_max", "mdi:thermometer-high"),
    "pack_temp_min": _temp("pack_temp_min", "mdi:thermometer-low"),
    "shunt_temp": _temp("shunt_temp", "mdi:thermometer"),
    "inverter_ambient": _temp("inverter_ambient", "mdi:thermometer"),
    "controller_ambient": _temp("controller_ambient", "mdi:thermometer"),
    "fan_a_rpm": _fan("fan_a_rpm", REVOLUTIONS_PER_MINUTE, "mdi:fan", 0),
    "fan_b_rpm": _fan("fan_b_rpm", REVOLUTIONS_PER_MINUTE, "mdi:fan", 0),
    "fan_rpm": _fan("fan_rpm", REVOLUTIONS_PER_MINUTE, "mdi:fan", 0),
    "fan_target_rpm": _fan("fan_target_rpm", REVOLUTIONS_PER_MINUTE, "mdi:speedometer", 0),
    "fan_a_duty": _fan("fan_a_duty", PERCENTAGE, "mdi:percent", 1),
    "fan_b_duty": _fan("fan_b_duty", PERCENTAGE, "mdi:percent", 1),
}


# ---------------------------------------------------------------------------
#  Remote / Neurio meter CT fields (2026-10-10):
#  (key, label, CT metric, unit, device class, state class, converter, enabled, kinds)
# ---------------------------------------------------------------------------
REMOTE_METER_FIELDS = (
    ("power", "Power", "InstRealPower", UnitOfPower.WATT, SensorDeviceClass.POWER,
     SensorStateClass.MEASUREMENT, None, True, ("trm", "neurio")),
    ("voltage", "Voltage", "InstVoltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,
     SensorStateClass.MEASUREMENT, None, True, ("trm", "neurio")),
    ("current", "Current", "InstCurrent", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT,
     SensorStateClass.MEASUREMENT, None, True, ("trm", "neurio")),
    ("reactive_power", "Reactive Power", "InstReactivePower", UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
     SensorDeviceClass.REACTIVE_POWER, SensorStateClass.MEASUREMENT, None, False, ("trm", "neurio")),
    ("energy_imported", "Energy Imported", "EnergyImportedWs", UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY,
     SensorStateClass.TOTAL_INCREASING, ws_to_wh, True, ("trm",)),
    ("energy_exported", "Energy Exported", "EnergyExportedWs", UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY,
     SensorStateClass.TOTAL_INCREASING, ws_to_wh, True, ("trm",)),
)
