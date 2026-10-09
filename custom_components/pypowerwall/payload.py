"""Pure helpers that normalise proxy / pypowerwall-server payloads (added 2026-10-10).

Nothing here touches Home Assistant runtime objects, so everything is cheap to
unit-test. The coordinator stores the derived structures in its data dict:

  data["packs"]         {serial: merged battery_blocks + /pod fields}
  data["unit_signals"]  {serial: {metric_id: value}}  temperatures / fans
  data["remote_meters"] {din: {"kind": "trm"|"neurio", "cts": {idx: {metric: value}}}}
"""
from __future__ import annotations

import math
import re
from typing import Any

# --------------------------------------------------------------------------- health
SERVER_DEGRADED_STATES = frozenset({"degraded", "unhealthy", "no_gateways"})


def is_server_health(health: Any) -> bool:
    """pypowerwall-server /health carries gateway_ids, the proxy's does not."""
    return isinstance(health, dict) and "gateway_ids" in health


def health_degraded(health: Any) -> bool | None:
    """Degraded connection to the gateway, for either backend (None = unknown).

    proxy:  connection_health.is_degraded (only when PW_HEALTH_CHECK is on)
    server: status "degraded" / "unhealthy" (some / all gateways offline)
    """
    if not isinstance(health, dict) or not health:
        return None
    if is_server_health(health):
        status = health.get("status")
        return None if status is None else status in SERVER_DEGRADED_STATES
    conn = health.get("connection_health")
    if not isinstance(conn, dict):
        return None
    return bool(conn.get("is_degraded"))


def health_fallback(health: Any) -> bool | None:
    """SolarOnly fallback mode, for either backend (None = unknown).

    proxy:  fallback_mode = {"is_fallback_mode": bool, ...}
    server: fallback_mode = {gateway_id: {"is_fallback_mode": bool, ...}, ...}
    """
    fb = health.get("fallback_mode") if isinstance(health, dict) else None
    if not isinstance(fb, dict):
        return None
    if "is_fallback_mode" in fb:
        return bool(fb.get("is_fallback_mode"))
    if is_server_health(health):
        return any(isinstance(v, dict) and v.get("is_fallback_mode") for v in fb.values())
    return None


FALLBACK_ATTRS = ("fallback_since", "fallback_duration_seconds", "recovery_attempts", "next_attempt_at")


def health_fallback_details(health: Any) -> dict[str, Any]:
    """Attributes of the fallback state; per gateway for pypowerwall-server."""
    fb = health.get("fallback_mode") if isinstance(health, dict) else None
    if not isinstance(fb, dict):
        return {}
    if "is_fallback_mode" in fb or not is_server_health(health):
        return {k: fb.get(k) for k in FALLBACK_ATTRS}
    return {
        "gateways": {
            gw: {k: v.get(k) for k in ("is_fallback_mode", *FALLBACK_ATTRS)}
            for gw, v in fb.items()
            if isinstance(v, dict)
        }
    }


def health_version(health: Any) -> str | None:
    """Version string of the proxy ("0.16.4 Proxy t99") or pypowerwall-server."""
    if not isinstance(health, dict):
        return None
    if health.get("pypowerwall"):
        return health["pypowerwall"]
    if is_server_health(health) and health.get("version"):
        return f"pypowerwall-server {health['version']}"
    return None


def server_gateways_online(health: Any) -> bool | None:
    """pypowerwall-server: every configured gateway online (None for the proxy)."""
    if not is_server_health(health):
        return None
    total = health.get("gateways")
    online = health.get("gateways_online")
    if isinstance(total, int) and isinstance(online, int):
        return total > 0 and online == total
    details = health.get("gateway_details")
    if isinstance(details, list) and details:
        return all(isinstance(d, dict) and d.get("online") for d in details)
    return None


# --------------------------------------------------------------------------- battery level
TESLA_RESERVE_FLOOR = 5.0


def raw_to_app_percent(raw: float | None) -> float | None:
    """Raw 5-100 % state of energy -> the Tesla app's 0-100 % display scale."""
    if raw is None:
        return None
    return max(0.0, min(100.0, (float(raw) - TESLA_RESERVE_FLOOR) / (100.0 - TESLA_RESERVE_FLOOR) * 100.0))


def soe_raw(js: dict[str, Any]) -> float | None:
    """Raw state of energy from /json.

    The proxy's /json.soe is raw; pypowerwall-server's /json.soe is already
    app-scaled and the raw value is in soe_raw.
    """
    if "soe_raw" in js:
        return js.get("soe_raw")
    return js.get("soe")


def soe_app(js: dict[str, Any]) -> float | None:
    """App-scaled state of energy from /json (server reports it, proxy -> computed)."""
    if "soe_raw" in js:
        return js.get("soe")
    return raw_to_app_percent(js.get("soe"))


# --------------------------------------------------------------------------- packs
POD_VITALS_FLAGS = (
    "POD_ActiveHeating",
    "POD_ChargeComplete",
    "POD_ChargeRequest",
    "POD_DischargeComplete",
    "POD_PermanentlyFaulted",
    "POD_PersistentlyFaulted",
    "POD_enable_line",
    "POD_available_charge_power",
    "POD_available_dischg_power",
)


def _serial_of(key: Any) -> str | None:
    if isinstance(key, str) and "--" in key:
        return key.rsplit("--", 1)[-1] or None
    return None


def parse_pod_groups(pod_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Split the flat /pod payload into its PW1_, PW2_ ... groups (prefix stripped)."""
    if not isinstance(pod_data, dict):
        return []
    groups: dict[int, dict[str, Any]] = {}
    for key, value in pod_data.items():
        m = re.match(r"^PW(\d+)_(.+)$", key) if isinstance(key, str) else None
        if m:
            groups.setdefault(int(m.group(1)), {})[m.group(2)] = value
    return [groups[i] for i in sorted(groups)]


def build_packs(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-pack data keyed by serial, correctly matched across sources.

    The pypowerwall proxy's /pod numbers PWn by *battery_blocks* order for the
    block fields (PackageSerialNumber, wobble_detected, backup_ready, ...) but
    by *vitals TEPOD* order for the POD_* fields, and the two orders differ on
    real systems. So POD_* fields are matched by the serial in PWn_name, block
    fields by PWn_PackageSerialNumber. Precedence: vitals TEPOD > /pod for POD_*,
    system_status.battery_blocks > /pod for block fields.
    """
    packs: dict[str, dict[str, Any]] = {}
    ss = data.get("system_status") or {}
    for block in ss.get("battery_blocks") or []:
        if isinstance(block, dict) and block.get("PackageSerialNumber"):
            packs.setdefault(block["PackageSerialNumber"], {}).update(
                {k: v for k, v in block.items() if v is not None}
            )

    for group in parse_pod_groups(data.get("pod")):
        block_serial = group.get("PackageSerialNumber")
        if block_serial:
            pack = packs.setdefault(block_serial, {})
            for k, v in group.items():
                if k.startswith("POD_") or k == "name" or v is None:
                    continue
                pack.setdefault(k, v)
        name_serial = _serial_of(group.get("name"))
        if name_serial:
            pack = packs.setdefault(name_serial, {})
            for k, v in group.items():
                if k.startswith("POD_") and v is not None:
                    pack[k] = v

    for key, block in (data.get("vitals") or {}).items():
        if not (isinstance(key, str) and key.startswith("TEPOD") and isinstance(block, dict)):
            continue
        serial = _serial_of(key)
        if not serial:
            continue
        pack = packs.setdefault(serial, {})
        for k in POD_VITALS_FLAGS:
            if block.get(k) is not None:
                pack[k] = block[k]
    return packs


# --------------------------------------------------------------------------- temperatures / fans
# Same metric ids and source signals as pypowerwall-server (app/core/signals.py)
# so entities line up with its MQTT discovery.
UNIT_SIGNALS: dict[str, tuple[str, ...]] = {
    "pack_temp_max": ("HVP_PackTempMax",),
    "pack_temp_min": ("HVP_PackTempMin",),
    "shunt_temp": ("HVP_ShuntTemperature",),
    "inverter_ambient": ("PCH_AmbientTemp",),
    "controller_ambient": ("THC_AmbientTemp",),
    "fan_a_rpm": ("PCH_FanSpeed_A",),
    "fan_b_rpm": ("PCH_FanSpeed_B",),
    "fan_rpm": ("PVAC_Fan_Speed_Actual_RPM",),
    "fan_target_rpm": ("PVAC_Fan_Speed_Target_RPM",),
    "fan_a_duty": ("PCH_FanDuty_A",),
    "fan_b_duty": ("PCH_FanDuty_B",),
}
_SIGNAL_TO_METRIC = {sig: metric for metric, sigs in UNIT_SIGNALS.items() for sig in sigs}
_UNIT_PREFIXES = ("TEPOD", "TEPINV", "TETHC", "PVAC")


def numeric(value: Any) -> float | None:
    """Finite float or None (rejects bools, strings, NaN/inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def build_unit_signals(vitals: Any, fans: Any = None) -> dict[str, dict[str, float | None]]:
    """Per Powerwall unit (serial) temperature and fan readings.

    A metric is listed as soon as the unit *reports the signal* (value may be
    None for a poll), so entities are not lost when a reading is briefly null.
    Vitals win over /fans. A unit reporting Powerwall 3 fans (PCH_FanSpeed_A/B)
    never also gets PVAC (Powerwall 2/+) fan entities. Note: TEPINV blocks exist
    for Powerwall 2/+ too, so their presence alone does not mean Powerwall 3.
    """
    sources = [s for s in (vitals, fans) if isinstance(s, dict)]
    units: dict[str, dict[str, float | None]] = {}
    for source in sources:
        for key, block in source.items():
            if not (isinstance(key, str) and isinstance(block, dict)):
                continue
            prefix = key.split("--", 1)[0]
            serial = _serial_of(key)
            if prefix not in _UNIT_PREFIXES or not serial:
                continue
            signals = units.setdefault(serial, {})
            for field, value in block.items():
                metric = _SIGNAL_TO_METRIC.get(field)
                if metric is None:
                    continue
                val = numeric(value)
                if signals.get(metric) is None:
                    signals[metric] = val
    for signals in units.values():
        if "fan_a_rpm" in signals or "fan_b_rpm" in signals:
            signals.pop("fan_rpm", None)
            signals.pop("fan_target_rpm", None)
    return {s: v for s, v in units.items() if v}


# --------------------------------------------------------------------------- remote meters
_CT_FIELD = re.compile(r"^(TRM|NEURIO)_CT(\d+)_(.+)$")


def build_remote_meters(vitals: Any) -> dict[str, dict[str, Any]]:
    """Tesla remote meters (TRM--<din>) and Neurio meters (NEURIO--<sn>) from vitals."""
    meters: dict[str, dict[str, Any]] = {}
    if not isinstance(vitals, dict):
        return meters
    for key, block in vitals.items():
        if not (isinstance(key, str) and isinstance(block, dict)):
            continue
        if key.startswith("TRM--"):
            kind = "trm"
        elif key.startswith("NEURIO--"):
            kind = "neurio"
        else:
            continue
        ident = key.split("--", 1)[1]
        if not ident:
            continue
        cts: dict[str, dict[str, Any]] = {}
        for field, value in block.items():
            m = _CT_FIELD.match(field) if isinstance(field, str) else None
            if m:
                cts.setdefault(m.group(2), {})[m.group(3)] = value
        if cts:
            meters[ident] = {"kind": kind, "cts": cts}
    return meters


def ws_to_wh(value: Any) -> float | None:
    """Watt-seconds -> Wh (remote meter energy counters); 0/None -> None."""
    val = numeric(value)
    return round(val / 3600, 3) if val else None
