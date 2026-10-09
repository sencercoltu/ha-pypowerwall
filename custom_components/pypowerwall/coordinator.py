from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MAX_BACKUP_MINUTES,
    CONF_PACK_COUNT,
    DEFAULT_MAX_BACKUP_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .const import BACKEND_PROXY, BACKEND_SERVER  # 2026-09-29: pypowerwall-server support
from .sensor_descriptions import observed_pack_count

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# (result key, path, required)
# Required endpoints raise UpdateFailed on error; optional ones resolve to None.
ENDPOINTS: tuple[tuple[str, str, bool], ...] = (
    ("aggregates", "/aggregates", True),
    ("vitals", "/vitals", True),
    ("health", "/health", True),
    ("json", "/json", False),
    ("version_info", "/version", False),
    ("operation", "/api/operation", False),
    ("system_status", "/api/system_status", False),
    ("sitemaster", "/api/sitemaster", False),
    ("pod", "/pod", False),
    ("troubleshooting", "/api/troubleshooting/problems", False),
    ("stats", "/stats", False),
    ("gateway_status", "/api/status", False),
    ("grid_status", "/api/system_status/grid_status", False),
    ("meters_site", "/api/meters/site", False),
    ("meters_solar", "/api/meters/solar", False),
    # Control state (only meaningful when PW_CONTROL_SECRET is set on the proxy)
    ("control_grid_charging", "/control/grid_charging", False),
    ("control_grid_export", "/control/grid_export", False),
    ("control_max_backup", "/control/max_backup", False),
)

# Keys whose value should default to {} rather than None when missing.
DICT_DEFAULT_KEYS = frozenset(
    {"json", "aggregates", "vitals", "health", "version_info", "system_status", "sitemaster"}
)

# Endpoints whose content changes rarely: polled every SLOW_POLL_EVERY refreshes,
# the previous value is reused in between. Cuts ~1/3 of the requests per cycle.
SLOW_POLL_KEYS = frozenset(
    {"version_info", "gateway_status", "stats", "sitemaster", "troubleshooting", "meters_solar"}
)
SLOW_POLL_EVERY = 10

# ---------------------------------------------------------------- pypowerwall-server (2026-09-29)
# GET endpoints only the pypowerwall proxy implements. pypowerwall-server has just
# POST /control/{path} (a GET there is 405); it reports grid charging / grid export
# in /api/operation instead, and has no max-backup control at all.
PROXY_ONLY_KEYS = frozenset({"control_grid_charging", "control_grid_export", "control_max_backup"})

# pypowerwall-server waits up to 10 s on its own cloud/local call before answering,
# so a 10 s client timeout could report a slow-but-successful write as failed.
SERVER_WRITE_TIMEOUT = aiohttp.ClientTimeout(total=20)

# pypowerwall-server answers /api/operation from its poll cache (PW_CACHE_EXPIRE,
# default 5 s): the refresh right after a write usually still sees the old value,
# so schedule a second one once the server has re-polled the gateway.
SERVER_SETTLE_SECONDS = 8


def detect_backend(health: Any) -> str | None:
    """Tell pypowerwall-server and the pypowerwall proxy apart by their /health payload.

    proxy:  {"pypowerwall": "0.x.y Proxy tNN", "mode": ..., "transports": ...}
    server: {"status": ..., "version": ..., "gateways": N, "gateway_ids": [...], ...}
    """
    if not isinstance(health, dict) or not health:
        return None
    if "gateway_ids" in health:
        return BACKEND_SERVER
    if "pypowerwall" in health:
        return BACKEND_PROXY
    return None


def server_command_value(action: str, value: Any) -> Any:
    """Convert a proxy-style command value to the JSON type pypowerwall-server validates.

    The proxy takes every value as a form string; the server rejects anything but
    a JSON bool for grid_charging and a JSON int for reserve (HTTP 400).
    """
    if action == "grid_charging":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "false"):
            return text == "true"
        raise ValueError(f"grid_charging expects true/false, got {value!r}")
    if action == "reserve":
        if isinstance(value, bool):
            raise ValueError(f"reserve expects an integer, got {value!r}")
        return int(value)
    return str(value)


class PyPowerwallCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls the pypowerwall proxy."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        control_secret: str = "",
        max_backup_minutes: int = DEFAULT_MAX_BACKUP_MINUTES,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
            config_entry=config_entry,
        )
        self.host = host
        self.port = port
        self._base_url = f"http://{host}:{port}"
        self._control_secret = control_secret
        self._session = async_get_clientsession(hass)
        self._max_backup_minutes = int(max_backup_minutes)
        self._cycle = 0
        self._backend: str | None = None  # 2026-09-29: BACKEND_PROXY / BACKEND_SERVER, None = not detected yet
        self._settle_unsub: Any = None  # 2026-09-29: pending post-write refresh (pypowerwall-server)
        _LOGGER.debug(
            "Coordinator initialised: base_url=%s interval=%ss control=%s",
            self._base_url,
            scan_interval,
            "enabled" if control_secret else "disabled",
        )

    @property
    def base_url(self) -> str:
        """Return the proxy base URL."""
        return self._base_url

    @property
    def has_control_secret(self) -> bool:
        """Return True if a control secret is configured."""
        return bool(self._control_secret)

    # ------------------------------------------------------- backend (2026-09-29)
    @property
    def backend(self) -> str | None:
        """BACKEND_PROXY or BACKEND_SERVER, None until /health has been seen."""
        return self._backend

    @property
    def is_server(self) -> bool:
        """True when talking to pypowerwall-server rather than the pypowerwall proxy."""
        return self._backend == BACKEND_SERVER

    @property
    def supports_max_backup(self) -> bool:
        """pypowerwall-server has no /control/max_backup (as of server 0.7.x)."""
        return not self.is_server

    # ------------------------------------------------------- max backup duration
    @property
    def max_backup_minutes(self) -> int:
        """Duration used when the max-backup switch is turned on."""
        return self._max_backup_minutes

    def set_max_backup_minutes(self, minutes: int, *, persist: bool = True) -> None:
        """Set (and by default persist in the config entry options) the max-backup duration."""
        self._max_backup_minutes = int(minutes)
        entry = self.config_entry
        if persist and entry is not None and entry.options.get(CONF_MAX_BACKUP_MINUTES) != int(minutes):
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_MAX_BACKUP_MINUTES: int(minutes)}
            )

    def matches(self, scan_interval: int, control_secret: str) -> bool:
        """True if this coordinator was built with these connection settings."""
        running = int(self.update_interval.total_seconds()) if self.update_interval else None
        return running == int(scan_interval) and self._control_secret == control_secret

    @property
    def max_backup_duration(self) -> int:
        """Duration in seconds (what the proxy expects)."""
        return self._max_backup_minutes * 60

    # ------------------------------------------------------- max backup state
    @property
    def max_backup_active(self) -> bool | None:
        """True while a manual (max) backup event is active, None if unknown.

        The gateway leaves expired events lingering, so 'manual_backup present'
        is not enough: use its 'active' flag (or end_time when missing).
        """
        data = self.data.get("control_max_backup") if self.data else None
        if not isinstance(data, dict) or "manual_backup" not in data:
            return None
        mb = data.get("manual_backup")
        if not mb:
            return False
        if "active" in mb:
            return bool(mb["active"])
        end = mb.get("end_time")
        if end is not None:
            return time.time() < float(end)
        return True

    def _path_for(self, path: str) -> str:
        """Add the control token to endpoints where the proxy uses it for GET.

        /control/max_backup: with a valid token the proxy auto-cancels expired
        manual backup events that the gateway leaves lingering; a plain GET is
        read-only and keeps reporting the stale event.
        """
        if path == "/control/max_backup" and self._control_secret:
            return f"{path}?token={self._control_secret}"
        return path

    def _due(self, key: str) -> bool:
        """Whether this endpoint should be fetched on this cycle."""
        # 2026-09-29: pypowerwall-server has no GET /control/*; never poll (or send the token) there
        if key in PROXY_ONLY_KEYS and self.is_server:
            return False
        if key not in SLOW_POLL_KEYS or self._cycle % SLOW_POLL_EVERY == 0:
            return True
        # not due: only skip if we already have a value from an earlier cycle
        return self.data is None or key not in self.data

    async def _async_update_data(self) -> dict[str, Any]:
        # 2026-09-29: identify the backend before the first poll, so proxy-only
        # endpoints (incl. the token-in-query GET /control/max_backup) never reach
        # pypowerwall-server. /health is fetched first (still required) and reused below.
        prefetched: dict[str, Any] = {}
        if self._backend is None:
            prefetched["health"] = await self._fetch("/health", True)
            self._backend = detect_backend(prefetched["health"])
            if self._backend is not None:
                _LOGGER.debug("Backend at %s detected as %s", self._base_url, self._backend)
        # [OLD 2026-09-29] replaced: skip endpoints already prefetched above. Delete when happy.
        # todo = [(key, path, required) for key, path, required in ENDPOINTS if self._due(key)]
        # [/OLD 2026-09-29]
        todo = [
            (key, path, required)
            for key, path, required in ENDPOINTS
            if key not in prefetched and self._due(key)
        ]
        results = await asyncio.gather(
            *(self._fetch(self._path_for(path), required) for _key, path, required in todo)
        )
        self._cycle += 1

        data: dict[str, Any] = {}
        fetched = {key: value for (key, _p, _r), value in zip(todo, results, strict=True)}
        fetched.update(prefetched)  # 2026-09-29: /health fetched before the gather
        for key, _path, _required in ENDPOINTS:
            # slow-poll key not due this cycle -> carry the previous value
            value = fetched[key] if key in fetched else (self.data or {}).get(key)
            data[key] = value if value is not None or key not in DICT_DEFAULT_KEYS else {}

        # Failsafe: if all core endpoints came back empty, the proxy is not usable.
        if not any(data[k] for k in ("aggregates", "vitals", "health")):
            raise UpdateFailed("All core endpoints returned no data")

        _LOGGER.debug(
            "Refreshed %d/%d endpoints from %s (%d without data)",
            len(todo),
            len(ENDPOINTS),
            self._base_url,
            sum(1 for v in results if v is None),
        )
        # 2026-09-29: keep following /health in case the host:port now runs the other backend
        self._backend = detect_backend(data["health"]) or self._backend
        data["pack_count"] = self._sticky_pack_count(data)
        self._update_repairs(data)
        return data

    def _sticky_pack_count(self, data: dict[str, Any]) -> int:
        """Max number of packs ever observed (persisted), so a degraded transport
        that hides some Powerwalls does not skew per-pack maths."""
        seen = observed_pack_count(data)
        remembered = int((self.config_entry.options.get(CONF_PACK_COUNT) if self.config_entry else 0) or 0)
        best = max(seen, remembered)
        if best > remembered and self.config_entry is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, options={**self.config_entry.options, CONF_PACK_COUNT: best}
            )
        return best

    # ------------------------------------------------------------------ repairs
    def _update_repairs(self, data: dict[str, Any]) -> None:
        """Raise / clear repair issues from /health."""
        health = data.get("health") or {}
        degraded = bool((health.get("connection_health") or {}).get("is_degraded"))
        fallback = bool((health.get("fallback_mode") or {}).get("is_fallback_mode"))
        self._set_issue("proxy_degraded", degraded, {"host": self.host})
        self._set_issue("proxy_fallback", fallback, {"host": self.host})

    def _set_issue(self, key: str, active: bool, placeholders: dict[str, str]) -> None:
        issue_id = f"{key}_{self.host}_{self.port}"
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=placeholders,
                learn_more_url="https://github.com/jasonacox/pypowerwall/tree/main/proxy#health-check",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _fetch(self, path: str, required: bool) -> Any:
        """GET one proxy endpoint and return its parsed JSON.

        Required endpoints raise UpdateFailed on any failure. Optional
        endpoints return None on 404 / errors so the rest of the update
        can proceed. A 401/403 always means the proxy rejected our request
        (bad or missing control secret) -> reauth.
        """
        url = f"{self._base_url}{path}"
        path = path.split("?", 1)[0]  # never log / raise the query string (may carry the token)
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status in (401, 403):
                    raise ConfigEntryAuthFailed(
                        f"pypowerwall proxy rejected request to {path} (HTTP {resp.status})"
                    )
                if resp.status == 404 and not required:
                    return None
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except ConfigEntryAuthFailed:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            if required:
                raise UpdateFailed(
                    f"Error communicating with pypowerwall proxy ({path}): {err}"
                ) from err
            _LOGGER.debug("Optional endpoint %s unavailable: %s", path, err)
            return None
        except ValueError as err:  # invalid JSON
            if required:
                raise UpdateFailed(f"Invalid JSON from {path}: {err}") from err
            _LOGGER.debug("Optional endpoint %s returned invalid JSON: %s", path, err)
            return None

    async def send_command(self, path: str, value: str | int | float) -> bool:
        """POST a control command to the proxy.

        Uses form data with value + token as expected by pypowerwall proxy:
          curl -X POST -d "value=VALUE&token=SECRET" http://host:port/control/...
        """
        if not self._control_secret:
            _LOGGER.error("Control secret not configured - cannot send command")
            return False
        # 2026-09-29: pypowerwall-server wants a JSON body and a Bearer token instead
        if self.is_server:
            return await self._send_command_server(path, value)
        url = f"{self._base_url}{path}"
        form_data = {"value": str(value), "token": self._control_secret}
        _LOGGER.debug("POST %s value=%s", url, value)
        try:
            async with self._session.post(
                url, data=form_data, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    _LOGGER.error(
                        "POST %s rejected (HTTP %s): check the control secret",
                        url,
                        resp.status,
                    )
                    return False
                resp.raise_for_status()
                return True
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("POST %s failed: %s", url, err)
            return False

    # ------------------------------------------------ pypowerwall-server writes (2026-09-29)
    async def _send_command_server(self, path: str, value: Any) -> bool:
        """POST a control command to pypowerwall-server.

          curl -X POST -H "Authorization: Bearer SECRET" -H "Content-Type: application/json" \\
               -d '{"value": 20}' http://host:port/control/reserve
        """
        action = path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if action == "max_backup":
            _LOGGER.error("pypowerwall-server has no max backup control (%s is proxy-only)", path)
            return False
        try:
            payload = {"value": server_command_value(action, value)}
        except (TypeError, ValueError) as err:
            _LOGGER.error("Not sending %s: %s", path, err)
            return False
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._control_secret}"}
        _LOGGER.debug("POST %s json=%s (pypowerwall-server)", url, payload)
        try:
            async with self._session.post(
                url, json=payload, headers=headers, timeout=SERVER_WRITE_TIMEOUT
            ) as resp:
                detail = _error_detail(await resp.text())
                if resp.status in (401, 403):
                    # 401 = wrong token, 403 = PW_CONTROL_SECRET not set on the server
                    _LOGGER.error(
                        "POST %s rejected (HTTP %s: %s): check PW_CONTROL_SECRET on pypowerwall-server",
                        url,
                        resp.status,
                        detail,
                    )
                    return False
                if resp.status >= 400:
                    _LOGGER.error("POST %s failed (HTTP %s): %s", url, resp.status, detail)
                    return False
                if detail.startswith("error:"):
                    _LOGGER.error("POST %s returned an error: %s", url, detail)
                    return False
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("POST %s failed: %s", url, err)
            return False
        self._schedule_settle_refresh()
        return True

    def _schedule_settle_refresh(self) -> None:
        """Refresh again once pypowerwall-server has re-polled the gateway."""
        if self._settle_unsub is not None:
            self._settle_unsub()

        async def _refresh(_now: Any) -> None:
            self._settle_unsub = None
            await self.async_request_refresh()

        self._settle_unsub = async_call_later(self.hass, SERVER_SETTLE_SECONDS, _refresh)

    async def async_shutdown(self) -> None:
        """Cancel a pending post-write refresh, then shut down as usual."""
        if self._settle_unsub is not None:
            self._settle_unsub()
            self._settle_unsub = None
        await super().async_shutdown()


def _error_detail(text: str) -> str:
    """Short human-readable reason from a pypowerwall-server response body.

    FastAPI errors are {"detail": ...}; a success body carrying {"error": ...}
    is reported as "error: ..." so the caller can treat it as a failure.
    """
    try:
        body = json.loads(text) if text else None
    except ValueError:
        return text[:300]
    if isinstance(body, dict):
        if "detail" in body:
            return str(body["detail"])[:300]
        if "error" in body:
            return f"error: {body['error']}"[:300]
        return ""
    return text[:300]
