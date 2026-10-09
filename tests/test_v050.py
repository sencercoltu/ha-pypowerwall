"""0.5.0 (2026-10-10): pypowerwall-server parity, new entities and bug fixes."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest

from custom_components.pypowerwall.const import DOMAIN
from custom_components.pypowerwall.entity import parse_pod_data
from custom_components.pypowerwall.payload import (
    build_packs,
    build_remote_meters,
    build_unit_signals,
    health_degraded,
    health_fallback,
    health_version,
    raw_to_app_percent,
    soe_app,
    soe_raw,
    ws_to_wh,
)

from .conftest import SECRET, load_fixture

GATEWAY = "TG0000000001AA"
EXPANSION = "TG0000000004DD"

SERVER_HEALTH = {
    "status": "healthy",
    "version": "0.7.3",
    "gateways": 1,
    "gateways_online": 1,
    "gateways_offline": 0,
    "gateway_ids": ["default"],
    "gateway_details": [{"id": "default", "online": True, "error": None}],
    "fallback_mode": {"default": {"is_fallback_mode": False, "fallback_since": None, "recovery_attempts": 0}},
}


async def _refresh(hass, entry):
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def _enable(hass, entity_id):
    reg = er.async_get(hass)
    reg.async_update_entity(entity_id, disabled_by=None)


def _as_server(proxy) -> None:
    """Turn the fake proxy into a pypowerwall-server."""
    proxy.data["/health"] = dict(SERVER_HEALTH)
    js = proxy.data["/json"]
    js["soe_raw"] = js["soe"]
    js["soe"] = raw_to_app_percent(js["soe"])
    proxy.data["/api/operation"] = {
        "real_mode": "self_consumption",
        "backup_reserve_percent": 20,
        "grid_charging": True,
        "grid_export": "pv_only",
        "stale": False,
        "last_updated": None,
    }
    for path in ("/control/grid_charging", "/control/grid_export", "/control/max_backup"):
        proxy.data.pop(path, None)


@pytest.fixture
async def server_entry(hass, proxy, make_entry):
    _as_server(proxy)
    entry = make_entry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ------------------------------------------------------------------ pure helpers
def test_health_helpers_both_backends():
    proxy_health = load_fixture("health.json")
    assert health_degraded(proxy_health) is False
    assert health_fallback(proxy_health) is False
    assert health_version(proxy_health) == proxy_health["pypowerwall"]

    assert health_degraded(SERVER_HEALTH) is False
    assert health_fallback(SERVER_HEALTH) is False
    assert health_version(SERVER_HEALTH) == "pypowerwall-server 0.7.3"
    bad = {**SERVER_HEALTH, "status": "degraded",
           "fallback_mode": {"a": {"is_fallback_mode": False}, "b": {"is_fallback_mode": True}}}
    assert health_degraded(bad) is True
    assert health_fallback(bad) is True
    assert health_degraded({}) is None
    assert health_fallback(None) is None


def test_soe_helpers():
    assert soe_raw({"soe": 50.0}) == 50.0  # proxy: soe is raw
    assert soe_app({"soe": 100.0}) == 100.0
    assert soe_app({"soe": 5.0}) == 0.0
    assert soe_app({"soe": 2.0}) == 0.0  # clamped
    assert soe_raw({"soe": 47.4, "soe_raw": 50.0}) == 50.0  # server
    assert soe_app({"soe": 47.4, "soe_raw": 50.0}) == 47.4
    assert ws_to_wh(3600) == 1.0
    assert ws_to_wh(0) is None


def test_pod_flags_are_matched_by_serial_not_position():
    """/pod numbers POD_* fields by vitals order but block fields by battery_blocks order."""
    pod = load_fixture("pod.json")
    # PW2 group: name -> 04DD (expansion), PackageSerialNumber -> 02BB
    assert pod["PW2_name"].endswith(EXPANSION) and pod["PW2_PackageSerialNumber"] == "TG0000000002BB"
    pod["PW2_POD_ActiveHeating"] = 1
    pod["PW2_wobble_detected"] = 1
    parsed = parse_pod_data(pod)
    assert parsed[EXPANSION]["POD_ActiveHeating"] == 1
    assert parsed["TG0000000002BB"]["POD_ActiveHeating"] == 0
    assert parsed["TG0000000002BB"]["wobble_detected"] == 1
    assert parsed[EXPANSION]["wobble_detected"] == 0

    packs = build_packs({"pod": pod, "system_status": load_fixture("api_system_status.json"), "vitals": {}})
    assert packs[EXPANSION]["POD_ActiveHeating"] == 1
    assert packs["TG0000000002BB"]["POD_ActiveHeating"] == 0
    assert packs["TG0000000002BB"]["wobble_detected"] == 1


def test_unit_signals_and_remote_meters():
    vitals = {
        "TEPOD--1707000-11-M--TG1": {"HVP_PackTempMax": 31.5, "HVP_PackTempMin": 22.0, "HVP_ShuntTemperature": None},
        "TEPINV--1707000-11-M--TG1": {"PCH_AmbientTemp": 40.1, "PCH_FanSpeed_A": 1200, "PCH_FanDuty_A": 33.3},
        "PVAC--1707000-11-M--TG1": {"PVAC_Fan_Speed_Actual_RPM": 999},  # PW3: ignored
        "TETHC--1092170-03-E--TG2": {"THC_AmbientTemp": 18.5},
        "TRM--2002069-00-E--EM1": {"TRM_CT0_InstRealPower": 158.3, "TRM_CT0_EnergyImportedWs": 7200,
                                   "TRM_CT0_Location": "solar", "firmwareVersion": "x"},
        "NEURIO--VAH123": {"NEURIO_CT1_InstVoltage": 240.1},
    }
    fans = {"PVAC--1092170-03-E--TG2": {"PVAC_Fan_Speed_Actual_RPM": 1500, "PVAC_Fan_Speed_Target_RPM": 1600}}
    units = build_unit_signals(vitals, fans)
    assert units["TG1"] == {
        "pack_temp_max": 31.5, "pack_temp_min": 22.0, "shunt_temp": None,
        "inverter_ambient": 40.1, "fan_a_rpm": 1200.0, "fan_a_duty": 33.3,
    }
    assert units["TG2"] == {"controller_ambient": 18.5, "fan_rpm": 1500.0, "fan_target_rpm": 1600.0}

    meters = build_remote_meters(vitals)
    assert meters["2002069-00-E--EM1"] == {
        "kind": "trm", "cts": {"0": {"InstRealPower": 158.3, "EnergyImportedWs": 7200, "Location": "solar"}},
    }
    assert meters["VAH123"]["kind"] == "neurio"


# ------------------------------------------------------------------ proxy entities
async def test_pod_health_flag_entity_follows_serial(hass, proxy, make_entry):
    proxy.data["/pod"]["PW2_POD_ActiveHeating"] = 1  # belongs to the expansion pack (PW2_name)
    entry = make_entry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    exp_id = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_{EXPANSION}_pod_health_pod_active_heating")
    bb_id = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_TG0000000002BB_pod_health_pod_active_heating")
    for eid in (exp_id, bb_id):
        _enable(hass, eid)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(exp_id).state == "on"
    assert hass.states.get(bb_id).state == "off"


async def test_inverter_power_is_kw(hass, proxy, setup_entry):
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("sensor", DOMAIN, f"{setup_entry.entry_id}_{GATEWAY}_inverter_power")
    st = hass.states.get(eid)
    # PINV_Pout -0.97 kW, shown in W
    assert st.attributes["unit_of_measurement"] == "W"
    assert float(st.state) == pytest.approx(-970)


async def test_battery_level_raw_and_app_on_proxy(hass, proxy, setup_entry):
    soe = load_fixture("json.json")["soe"]
    assert float(hass.states.get("sensor.pypowerwall_battery_level").state) == round(soe, 1)
    assert float(hass.states.get("sensor.pypowerwall_battery_level_app").state) == round(raw_to_app_percent(soe), 1)


async def test_grid_connected_unknown_without_json(hass, proxy, setup_entry):
    assert hass.states.get("binary_sensor.pypowerwall_grid_connected").state == "on"
    del proxy.data["/json"]
    await _refresh(hass, setup_entry)
    assert hass.states.get("binary_sensor.pypowerwall_grid_connected").state == "unknown"


async def test_pod_soc_unknown_when_energy_missing(hass, proxy, setup_entry):
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("sensor", DOMAIN, f"{setup_entry.entry_id}_{GATEWAY}_pod_soc")
    assert hass.states.get(eid).state not in ("unknown", "0.0")
    key = next(k for k in proxy.data["/vitals"] if k.startswith("TEPOD") and k.endswith(GATEWAY))
    proxy.data["/vitals"][key]["POD_nom_energy_remaining"] = None
    await _refresh(hass, setup_entry)
    assert hass.states.get(eid).state == "unknown"


async def test_grid_charging_and_export_read_only_on_proxy(hass, proxy, setup_entry):
    exp = load_fixture("control_grid_export.json")["grid_export"]
    assert hass.states.get("sensor.pypowerwall_grid_export_policy").state == exp
    gc = load_fixture("control_grid_charging.json")["grid_charging"]
    assert hass.states.get("binary_sensor.pypowerwall_grid_charging_allowed").state == ("on" if gc else "off")


async def test_temperature_fan_and_remote_meter_entities(hass, proxy, make_entry):
    vitals = proxy.data["/vitals"]
    vitals["TETHC--1092170-03-E--TG0000000001AA"] = {"THC_AmbientTemp": 21.5, "alerts": []}
    vitals["TRM--2002069-00-E--EM4260230B10BC"] = {
        "TRM_CT0_InstRealPower": 158.3, "TRM_CT0_InstVoltage": 122.7, "TRM_CT0_InstCurrent": 0.95,
        "TRM_CT0_InstReactivePower": 1.0, "TRM_CT0_EnergyImportedWs": 36_000_000, "TRM_CT0_EnergyExportedWs": 0,
        "TRM_CT0_Location": "solar",
    }
    proxy.data["/fans"] = {"PVAC--1707000-21-K--TG0000000001AA": {
        "PVAC_Fan_Speed_Actual_RPM": 1500, "PVAC_Fan_Speed_Target_RPM": 1600}}
    entry = make_entry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)

    def state(platform, uid):
        eid = reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{uid}")
        assert eid is not None, uid
        return hass.states.get(eid)

    t = state("sensor", f"{GATEWAY}_controller_ambient")
    assert float(t.state) == 21.5 and t.attributes["device_class"] == "temperature"
    assert float(state("sensor", f"{GATEWAY}_fan_rpm").state) == 1500
    assert float(state("sensor", f"{GATEWAY}_fan_target_rpm").state) == 1600

    din = "2002069-00-E--EM4260230B10BC"
    assert float(state("sensor", f"{din}_ct0_power").state) == pytest.approx(158.3)
    imp = state("sensor", f"{din}_ct0_energy_imported")
    assert float(imp.state) == pytest.approx(10000)  # 36e6 Ws = 10 kWh = 10000 Wh
    assert imp.attributes["state_class"] == "total_increasing"
    assert state("sensor", f"{din}_ct0_energy_exported").state == "unknown"  # 0 never resets the counter
    assert imp.attributes["location"] == "solar"


async def test_pack_sensors_created_enabled_only_with_data(hass, proxy, make_entry):
    proxy.data["/api/system_status"]["battery_blocks"][0]["energy_charged"] = 123456
    entry = make_entry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    charged = reg.async_get(reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{GATEWAY}_pack_energy_charged"))
    assert charged.disabled_by is None
    assert float(hass.states.get(charged.entity_id).state) == pytest.approx(123.456)  # kWh
    other = reg.async_get(
        reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_TG0000000002BB_pack_energy_charged")
    )
    assert other.disabled_by is not None  # null on this pack -> created disabled


async def test_services_ignore_unloaded_entries(hass, proxy, setup_entry, make_entry):
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "set_reserve", {"reserve": 10}, blocking=True)
    assert proxy.posts == []


async def test_islanding_unsupported_on_proxy(hass, proxy, setup_entry):
    assert hass.states.get("button.pypowerwall_go_off_grid") is None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)


# ------------------------------------------------------------------ pypowerwall-server
async def test_server_backend_entities(hass, proxy, server_entry):
    coord = server_entry.runtime_data.coordinator
    assert coord.is_server
    assert hass.states.get("sensor.pypowerwall_pypowerwall_version").state == "pypowerwall-server 0.7.3"
    raw = proxy.data["/json"]["soe_raw"]
    assert float(hass.states.get("sensor.pypowerwall_battery_level").state) == round(raw, 1)
    assert float(hass.states.get("sensor.pypowerwall_battery_level_app").state) == round(raw_to_app_percent(raw), 1)
    assert hass.states.get("binary_sensor.pypowerwall_gateways_online").state == "on"
    assert hass.states.get("binary_sensor.pypowerwall_grid_charging_allowed").state == "on"
    assert hass.states.get("sensor.pypowerwall_grid_export_policy").state == "pv_only"
    assert hass.states.get("binary_sensor.pypowerwall_proxy_degraded").state == "off"
    assert hass.states.get("binary_sensor.pypowerwall_proxy_fallback_mode").state == "off"
    # proxy-only GET endpoints are never polled on the server
    assert not any(p.startswith("/control/") for p in proxy.get_log)


async def test_server_degraded_and_fallback_raise_repairs(hass, proxy, server_entry):
    proxy.data["/health"] = {
        **SERVER_HEALTH,
        "status": "degraded",
        "gateways_online": 0,
        "gateway_details": [{"id": "default", "online": False, "error": "timeout"}],
        "fallback_mode": {"default": {"is_fallback_mode": True, "fallback_since": 1.0, "recovery_attempts": 2}},
    }
    await _refresh(hass, server_entry)
    assert hass.states.get("binary_sensor.pypowerwall_proxy_degraded").state == "on"
    fb = hass.states.get("binary_sensor.pypowerwall_proxy_fallback_mode")
    assert fb.state == "on"
    assert fb.attributes["gateways"]["default"]["recovery_attempts"] == 2
    assert hass.states.get("binary_sensor.pypowerwall_gateways_online").state == "off"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"proxy_degraded_{proxy.host}_{proxy.port}") is not None
    assert issues.async_get_issue(DOMAIN, f"proxy_fallback_{proxy.host}_{proxy.port}") is not None


async def test_server_islanding_buttons_and_services(hass, proxy, server_entry):
    reg = er.async_get(hass)
    for key in ("go_off_grid", "reconnect_grid"):
        ent = reg.async_get(reg.async_get_entity_id("button", DOMAIN, f"{server_entry.entry_id}_{key}"))
        assert ent.disabled_by is not None  # dangerous -> disabled by default

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": False}, blocking=True)
    assert proxy.json_posts == []

    await hass.services.async_call(DOMAIN, "go_off_grid", {"confirm": True}, blocking=True)
    assert proxy.json_posts[-1] == (
        "/control/islanding", {"value": "off_grid", "confirm": True}, f"Bearer {SECRET}"
    )
    await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)
    assert proxy.json_posts[-1] == ("/control/islanding", {"value": "on_grid"}, f"Bearer {SECRET}")

    proxy.post_status["/control/islanding"] = (409, {"detail": "An islanding command is still in progress"})
    with pytest.raises(HomeAssistantError, match="still in progress"):
        await hass.services.async_call(DOMAIN, "reconnect_grid", {}, blocking=True)

    # the button path
    eid = reg.async_get_entity_id("button", DOMAIN, f"{server_entry.entry_id}_reconnect_grid")
    _enable(hass, eid)
    await hass.config_entries.async_reload(server_entry.entry_id)
    await hass.async_block_till_done()
    proxy.post_status.clear()
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    assert proxy.json_posts[-1][1] == {"value": "on_grid"}


async def test_server_reserve_command_is_json(hass, proxy, server_entry):
    await hass.services.async_call(DOMAIN, "set_reserve", {"reserve": 30}, blocking=True)
    assert proxy.json_posts[-1] == ("/control/reserve", {"value": 30}, f"Bearer {SECRET}")
    await hass.services.async_call(DOMAIN, "set_grid_charging", {"enabled": False}, blocking=True)
    assert proxy.json_posts[-1] == ("/control/grid_charging", {"value": False}, f"Bearer {SECRET}")
    assert hass.states.get("switch.pypowerwall_max_backup") is None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_max_backup", {}, blocking=True)
