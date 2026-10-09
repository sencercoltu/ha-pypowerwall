from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

# [OLD 2026-10-10] replaced: two separate `from .const import` blocks fail ruff's
# isort check (I001) in CI; merged into one below. Delete this block when happy.
# from .const import (
#     CONF_CONTROL_SECRET,
#     CONF_SCAN_INTERVAL,
#     DEFAULT_PORT,
#     DEFAULT_SCAN_INTERVAL,
#     DOMAIN,
# )
# from .const import BACKEND_SERVER  # 2026-09-29: pypowerwall-server support
# [/OLD 2026-10-10]
from .const import (
    BACKEND_SERVER,  # 2026-09-29: pypowerwall-server support
    CONF_CONTROL_SECRET,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import detect_backend  # 2026-09-29: pypowerwall-server support

_LOGGER = logging.getLogger(__name__)

SECRET_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
SCAN_INTERVAL_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=5, max=300))


def _user_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Required(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): vol.Coerce(int),
            vol.Optional(
                CONF_SCAN_INTERVAL, default=d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): SCAN_INTERVAL_VALIDATOR,
            vol.Optional(CONF_CONTROL_SECRET, default=d.get(CONF_CONTROL_SECRET, "")): SECRET_SELECTOR,
        }
    )


async def validate_connection(
    hass: HomeAssistant, host: str, port: int, control_secret: str = ""
) -> str | None:
    """Probe the proxy. Return an error key, or None when everything is fine.

    - cannot_connect: proxy unreachable / not JSON
    - invalid_auth: control secret configured but the proxy rejects it
    """
    session = async_get_clientsession(hass)
    base = f"http://{host}:{port}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(f"{base}/api/system_status/soe", timeout=timeout) as resp:
            if resp.status != 200:
                return "cannot_connect"
            data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return "cannot_connect"

    if control_secret:
        # 2026-09-29: pypowerwall-server checks a Bearer token on a JSON POST instead
        if await _probe_backend(session, base, timeout) == BACKEND_SERVER:
            return await _validate_server_secret(session, base, control_secret, timeout)
        # /control/mode with a bogus value validates the token without changing
        # anything: the proxy checks the token before the value.
        try:
            async with session.post(
                f"{base}/control/mode",
                data={"value": "", "token": control_secret},
                timeout=timeout,
            ) as resp:
                if resp.status in (401, 403):
                    return "invalid_auth"
                if resp.status == 404:
                    return "control_unsupported"
        except (aiohttp.ClientError, TimeoutError):
            return "cannot_connect"
    return None


async def _probe_backend(
    session: aiohttp.ClientSession, base: str, timeout: aiohttp.ClientTimeout
) -> str | None:
    """2026-09-29: BACKEND_PROXY / BACKEND_SERVER from /health, None if unknown."""
    try:
        async with session.get(f"{base}/health", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return detect_backend(await resp.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def _validate_server_secret(
    session: aiohttp.ClientSession, base: str, control_secret: str, timeout: aiohttp.ClientTimeout
) -> str | None:
    """2026-09-29: validate the control secret against pypowerwall-server.

    The server checks the token before the body, so an empty mode is safe:
    good token -> 400 (invalid mode, nothing written), bad token -> 401,
    PW_CONTROL_SECRET not set on the server -> 403.
    """
    try:
        async with session.post(
            f"{base}/control/mode",
            json={"value": ""},
            headers={"Authorization": f"Bearer {control_secret}"},
            timeout=timeout,
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status in (403, 404):
                return "control_unsupported"
    except (aiohttp.ClientError, TimeoutError):
        return "cannot_connect"
    return None


class PyPowerwallConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for PyPowerwall."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            error = await validate_connection(
                self.hass, host, port, user_input.get(CONF_CONTROL_SECRET, "")
            )
            if error is None:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PyPowerwall ({host}:{port})",
                    data=user_input,
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host/port (and secret/interval) of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current = {**entry.data, **entry.options}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            error = await validate_connection(
                self.hass, host, port, user_input.get(CONF_CONTROL_SECRET, "")
            )
            if error is None:
                await self.async_set_unique_id(f"{host}:{port}")
                # A different host:port is allowed (that is the point of reconfigure),
                # but must not collide with another configured entry.
                for other in self._async_current_entries():
                    if other.entry_id != entry.entry_id and other.unique_id == f"{host}:{port}":
                        return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"{host}:{port}",
                    title=f"PyPowerwall ({host}:{port})",
                    data={**entry.data, **user_input},
                    # options must not shadow the values just entered
                    options={
                        k: v
                        for k, v in entry.options.items()
                        if k not in (CONF_SCAN_INTERVAL, CONF_CONTROL_SECRET)
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(user_input or current),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Proxy rejected the control secret -> ask for a new one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await validate_connection(
                self.hass,
                entry.data[CONF_HOST],
                entry.data[CONF_PORT],
                user_input.get(CONF_CONTROL_SECRET, ""),
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_CONTROL_SECRET: user_input.get(CONF_CONTROL_SECRET, "")},
                    options={k: v for k, v in entry.options.items() if k != CONF_CONTROL_SECRET},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_CONTROL_SECRET, default=""): SECRET_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> PyPowerwallOptionsFlow:
        return PyPowerwallOptionsFlow()


class PyPowerwallOptionsFlow(OptionsFlow):
    """Options flow to change scan interval / control secret."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            secret = user_input.get(CONF_CONTROL_SECRET, "")
            error = None
            if secret:
                error = await validate_connection(
                    self.hass, entry.data[CONF_HOST], entry.data[CONF_PORT], secret
                )
            if error is None:
                return self.async_create_entry(data=user_input)
            errors["base"] = error

        current_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        current_secret = entry.options.get(
            CONF_CONTROL_SECRET, entry.data.get(CONF_CONTROL_SECRET, "")
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current_interval): SCAN_INTERVAL_VALIDATOR,
                    vol.Optional(CONF_CONTROL_SECRET, default=current_secret): SECRET_SELECTOR,
                }
            ),
            errors=errors,
        )
