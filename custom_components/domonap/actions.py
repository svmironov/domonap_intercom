from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, API, CALL_CONTROLLER
from .util import find_last_call_sensor_entity_id
from .relay import end_active_call, open_relay, raise_for_relay_error
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

SERVICE_OPEN_RELAY_BY_DOOR_ID = "open_relay_by_door_id"
SERVICE_OPEN_RELAY_BY_KEY_ID = "open_relay_by_key_id"
SERVICE_OPEN_RELAY_BY_LAST_CALL_DOOR_ID = "open_relay_by_last_call_door_id"
SERVICE_SILENCE_ACTIVE_CALL = "silence_active_call"
SERVICE_REJECT_ACTIVE_CALL = "reject_active_call"

SERVICE_OPEN_RELAY_BY_DOOR_ID_SCHEMA = vol.Schema(
    {
        vol.Required("door_id"): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)

SERVICE_OPEN_RELAY_BY_KEY_ID_SCHEMA = vol.Schema(
    {
        vol.Required("key_id"): cv.string,
        vol.Optional("config_entry_id"): cv.string,
    }
)

SERVICE_OPEN_RELAY_BY_LAST_CALL_DOOR_ID_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("config_entry_id"): cv.string,
    }
)

SERVICE_ACTIVE_CALL_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
    }
)


def _select_entry_id(hass: HomeAssistant, requested_entry_id: str | None) -> str | None:
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        return None

    if requested_entry_id:
        entry_data = domain_data.get(requested_entry_id)
        return (
            requested_entry_id
            if isinstance(entry_data, dict) and entry_data.get(API) is not None
            else None
        )

    config_entries = [
        (entry_id, entry_data)
        for entry_id, entry_data in domain_data.items()
        if isinstance(entry_data, dict) and entry_data.get(API) is not None
    ]
    active_entries = [
        entry_id
        for entry_id, entry_data in config_entries
        if getattr(entry_data.get(API), "active_call_id", None)
    ]
    if len(active_entries) == 1:
        return active_entries[0]

    if len(config_entries) > 1:
        raise HomeAssistantError("Multiple Domonap accounts: specify config_entry_id")
    return config_entries[0][0] if config_entries else None


def _entry_runtime(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    value = hass.data.get(DOMAIN, {}).get(entry_id)
    return value if isinstance(value, dict) else {}


async def _answer_panel_call(api: Any) -> dict[str, Any] | None:
    """Answer the ringing panel SIP call (200 OK) without opening the door.

    Best effort only: a SIP problem must never block the call teardown that
    follows. Mirrors openDoorSilentlyAndEndCall(): the 200 OK wins the forked
    call and the intercom panel stops ringing without any media in HA.
    """
    answer = getattr(api, "_answer_active_sip_before_open", None)
    if not callable(answer):
        return None
    try:
        return await answer(force=True)
    except Exception:
        _LOGGER.debug("Panel SIP pre-answer failed", exc_info=True)
        return {"ok": False, "error": "exception"}


async def async_setup_actions(hass: HomeAssistant) -> None:
    """Register Domonap actions (services)."""

    async def handle_open_relay(call: ServiceCall, *, by_door: bool) -> None:
        entry_id = _select_entry_id(hass, call.data.get("config_entry_id"))
        if not entry_id:
            raise HomeAssistantError("No Domonap config entries are set up")
        runtime = _entry_runtime(hass, entry_id)
        result = await open_relay(
            hass, runtime[API], runtime.get(CALL_CONTROLLER),
            call.data["door_id" if by_door else "key_id"],
            by_door=by_door, source="home_assistant_relay",
        )
        raise_for_relay_error(result)

    async def handle_open_relay_by_door_id(call: ServiceCall) -> None:
        await handle_open_relay(call, by_door=True)

    async def handle_open_relay_by_key_id(call: ServiceCall) -> None:
        await handle_open_relay(call, by_door=False)

    async def handle_open_relay_by_last_call_door_id(call: ServiceCall) -> dict[str, Any]:
        """Open door based on last incoming call sensor state."""
        requested_entry_id: str | None = call.data.get("config_entry_id")
        entry_id = _select_entry_id(hass, requested_entry_id)
        if not entry_id:
            return {"status": "error", "reason": "no_config_entries"}

        api = _entry_runtime(hass, entry_id).get(API)
        if api is None:
            return {"status": "error", "reason": "api_unavailable", "config_entry_id": entry_id}

        entity_id: str | None = call.data.get("entity_id")
        if not entity_id:
            entity_id = find_last_call_sensor_entity_id(hass, entry_id)

        if not entity_id:
            return {"status": "error", "reason": "sensor_not_found", "config_entry_id": entry_id}

        registered = er.async_get(hass).async_get(entity_id)
        if registered is None or registered.config_entry_id != entry_id:
            return {"status": "error", "reason": "sensor_account_mismatch", "entity_id": entity_id}

        st = hass.states.get(entity_id)
        if st is None:
            return {"status": "error", "reason": "sensor_not_found", "entity_id": entity_id}

        if st.state in ("unknown", "unavailable", "none", "None", ""):
            return {"status": "skipped", "reason": "no_last_call", "entity_id": entity_id, "state": st.state}

        door_id = st.state
        attrs = st.attributes or {}
        door_name = None
        try:
            door_name = (
                attrs.get("DoorName")
                or attrs.get("door_name")
                or attrs.get("Address")
                or attrs.get("Body")
                or attrs.get("Title")
            )
        except Exception:
            door_name = None

        runtime = _entry_runtime(hass, entry_id)
        result = await open_relay(
            hass, api, runtime.get(CALL_CONTROLLER), door_id,
            by_door=True, source="home_assistant_relay",
        )
        res = result["response"]
        ok = isinstance(res, dict) and res.get("ok") is True
        call_id = result["call_id"]
        end_call_result = result["end_call_result"]

        return {
            "status": "ok" if ok else "error",
            "door_id": door_id,
            "door_name": door_name,
            "call_id": call_id or None,
            "end_call_result": end_call_result,
            "entity_id": entity_id,
            "config_entry_id": entry_id,
            "response": res,
        }

    async def handle_active_call(
        call: ServiceCall, *, silence: bool
    ) -> dict[str, Any]:
        entry_id = _select_entry_id(hass, call.data.get("config_entry_id"))
        if not entry_id:
            return {"status": "error", "reason": "no_config_entries"}

        runtime = _entry_runtime(hass, entry_id)
        api = runtime.get(API)
        controller = runtime.get(CALL_CONTROLLER)
        if api is None:
            return {"status": "error", "reason": "api_unavailable", "config_entry_id": entry_id}

        call_id = getattr(api, "active_call_id", None)
        answered: Any = None
        if silence and (controller is None or not controller.external_call_established):
            answered = await _answer_panel_call(api)

        source = "home_assistant_silence" if silence else "home_assistant_reject"
        end_result = await end_active_call(
            hass, api, controller, source=source, expected_call_id=call_id
        )

        ok = isinstance(end_result, dict) and end_result.get("ok") is True
        response = {
            "status": "ok" if ok else ("skipped" if end_result is None else "error"),
            "call_id": call_id or None,
            "end_call_result": end_result,
            "config_entry_id": entry_id,
        }
        if silence:
            response["answered"] = isinstance(answered, dict) and answered.get("ok") is True
        return response

    async def handle_silence_active_call(call: ServiceCall) -> dict[str, Any]:
        """Answer (200 OK wins the fork and mutes the panel), then end the call."""
        return await handle_active_call(call, silence=True)

    async def handle_reject_active_call(call: ServiceCall) -> dict[str, Any]:
        """End without answering (603 while ringing); other branches may ring."""
        return await handle_active_call(call, silence=False)

    hass.services.async_register(
        DOMAIN,
        SERVICE_OPEN_RELAY_BY_DOOR_ID,
        handle_open_relay_by_door_id,
        schema=SERVICE_OPEN_RELAY_BY_DOOR_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_OPEN_RELAY_BY_KEY_ID,
        handle_open_relay_by_key_id,
        schema=SERVICE_OPEN_RELAY_BY_KEY_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_OPEN_RELAY_BY_LAST_CALL_DOOR_ID,
        handle_open_relay_by_last_call_door_id,
        schema=SERVICE_OPEN_RELAY_BY_LAST_CALL_DOOR_ID_SCHEMA,
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SILENCE_ACTIVE_CALL,
        handle_silence_active_call,
        schema=SERVICE_ACTIVE_CALL_SCHEMA,
        supports_response=True,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REJECT_ACTIVE_CALL,
        handle_reject_active_call,
        schema=SERVICE_ACTIVE_CALL_SCHEMA,
        supports_response=True,
    )


async def async_unload_actions(hass: HomeAssistant) -> None:
    """Unregister Domonap actions (services)."""
    for service in (
        SERVICE_OPEN_RELAY_BY_DOOR_ID,
        SERVICE_OPEN_RELAY_BY_KEY_ID,
        SERVICE_OPEN_RELAY_BY_LAST_CALL_DOOR_ID,
        SERVICE_SILENCE_ACTIVE_CALL,
        SERVICE_REJECT_ACTIVE_CALL,
    ):
        try:
            hass.services.async_remove(DOMAIN, service)
        except Exception:
            _LOGGER.debug("Failed to remove service %s.%s", DOMAIN, service, exc_info=True)
