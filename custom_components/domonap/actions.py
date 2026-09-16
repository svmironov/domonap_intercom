from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, API, CALL_CONTROLLER, EVENT_CALL_ENDED
from .util import extract_phone_digits

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

    return config_entries[0][0] if config_entries else None


def _find_last_call_sensor_entity_id(hass: HomeAssistant, entry_id: str | None) -> str | None:
    """Try to find last_call_door_id sensor entity_id."""
    if entry_id:
        try:
            entry = hass.config_entries.async_get_entry(entry_id)
        except Exception:
            entry = None

        if entry is not None:
            phone_digits = extract_phone_digits(entry)
            if phone_digits:
                candidate = f"sensor.{phone_digits}_last_call_door_id"
                if hass.states.get(candidate) is not None:
                    return candidate

        legacy = f"sensor.{DOMAIN}_{entry_id}_last_call_door_id"
        if hass.states.get(legacy) is not None:
            return legacy

    for st in hass.states.async_all("sensor"):
        if st.entity_id.endswith("_last_call_door_id") and st.entity_id.startswith("sensor."):
            return st.entity_id

    return None


async def _end_active_call(hass: HomeAssistant, api: Any) -> Any:
    """End an active call without making a successful door opening fail."""
    call_id = getattr(api, "active_call_id", None)
    if not call_id:
        return None

    try:
        result = await api.end_active_call()
    except Exception:
        _LOGGER.exception("Failed to end active call %s after opening the door", call_id)
        return {"ok": False, "error": "exception"}

    if result is not None and not (isinstance(result, dict) and result.get("ok") is True):
        _LOGGER.error("Failed to end active call %s after opening the door: %s", call_id, result)
    elif isinstance(result, dict) and result.get("ok") is True:
        _LOGGER.info("Active call %s ended after opening the door", call_id)
        hass.bus.fire(EVENT_CALL_ENDED, {"CallId": call_id})
    return result


def _entry_runtime(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    value = hass.data.get(DOMAIN, {}).get(entry_id)
    return value if isinstance(value, dict) else {}


async def _open_by_door_id(hass: HomeAssistant, entry_id: str, door_id: str) -> Any:
    runtime = _entry_runtime(hass, entry_id)
    controller = runtime.get(CALL_CONTROLLER)
    if controller is not None:
        return await controller.open_door_by_door_id(door_id)
    api = runtime.get(API)
    return await api.open_relay_by_door_id(door_id)


async def _open_by_key_id(hass: HomeAssistant, entry_id: str, key_id: str) -> Any:
    runtime = _entry_runtime(hass, entry_id)
    controller = runtime.get(CALL_CONTROLLER)
    if controller is not None:
        return await controller.open_door_by_key_id(key_id)
    api = runtime.get(API)
    return await api.open_relay_by_key_id(key_id)


async def _finish_relay_action(hass: HomeAssistant, entry_id: str, api: Any) -> Any:
    """Finish a successful relay action using the runtime's call policy.

    Rubetek Panel follows the APK behavior: opening the door ends the call. When
    an Asterisk dialog exists, the controller terminates that dialog and the
    temporary Domonap Panel SIP session together. Legacy phone/SMS entries keep
    using their existing API call termination path.
    """
    runtime = _entry_runtime(hass, entry_id)
    controller = runtime.get(CALL_CONTROLLER)
    if controller is not None:
        return await controller.end_after_relay(source="home_assistant_relay")
    return await _end_active_call(hass, api)


async def _abort_relay_call(hass: HomeAssistant, entry_id: str, api: Any) -> Any:
    """Best-effort call teardown after a failed relay opening.

    The mute-before-open answer may already have been sent, so a failed
    opening must not leave the accepted call hanging. Uses the runtime call
    policy when a controller exists so an external Asterisk dialog is torn
    down together with the Domonap call.
    """
    runtime = _entry_runtime(hass, entry_id)
    controller = runtime.get(CALL_CONTROLLER)
    if controller is not None:
        return await controller.end_call(source="relay_open_failed")
    return await _end_active_call(hass, api)


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

    async def handle_open_relay_by_door_id(call: ServiceCall) -> None:
        door_id: str = call.data["door_id"]
        requested_entry_id: str | None = call.data.get("config_entry_id")

        entry_id = _select_entry_id(hass, requested_entry_id)
        if not entry_id:
            _LOGGER.error("No Domonap config entries are set up")
            raise HomeAssistantError("No Domonap config entries are set up")

        api = _entry_runtime(hass, entry_id).get(API)
        if api is None:
            _LOGGER.error("Domonap API is not available for entry_id=%s", entry_id)
            raise HomeAssistantError(f"Domonap API is not available for entry_id={entry_id}")

        res: Any = await _open_by_door_id(hass, entry_id, door_id)
        if isinstance(res, dict) and res.get("ok") is True:
            _LOGGER.debug("Door relay opened (door_id=%s, entry_id=%s)", door_id, entry_id)
            await _finish_relay_action(hass, entry_id, api)
            return

        # The call may already be answered (mute-before-open); a failed relay
        # must not leave it hanging without a door behind it.
        await _abort_relay_call(hass, entry_id, api)
        _LOGGER.error("Failed to open relay by door_id=%s entry_id=%s: %s", door_id, entry_id, res)
        raise HomeAssistantError(f"Failed to open relay by door_id={door_id}")

    async def handle_open_relay_by_key_id(call: ServiceCall) -> None:
        key_id: str = call.data["key_id"]
        requested_entry_id: str | None = call.data.get("config_entry_id")

        entry_id = _select_entry_id(hass, requested_entry_id)
        if not entry_id:
            _LOGGER.error("No Domonap config entries are set up")
            raise HomeAssistantError("No Domonap config entries are set up")

        api = _entry_runtime(hass, entry_id).get(API)
        if api is None:
            _LOGGER.error("Domonap API is not available for entry_id=%s", entry_id)
            raise HomeAssistantError(f"Domonap API is not available for entry_id={entry_id}")

        res: Any = await _open_by_key_id(hass, entry_id, key_id)
        if isinstance(res, dict) and res.get("ok") is True:
            _LOGGER.debug("Door relay opened (key_id=%s, entry_id=%s)", key_id, entry_id)
            await _finish_relay_action(hass, entry_id, api)
            return

        # The call may already be answered (mute-before-open); a failed relay
        # must not leave it hanging without a door behind it.
        await _abort_relay_call(hass, entry_id, api)
        _LOGGER.error("Failed to open relay by key_id=%s entry_id=%s: %s", key_id, entry_id, res)
        raise HomeAssistantError(f"Failed to open relay by key_id={key_id}")

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
            entity_id = _find_last_call_sensor_entity_id(hass, entry_id)

        if not entity_id:
            return {"status": "error", "reason": "sensor_not_found", "config_entry_id": entry_id}

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

        res: Any = await _open_by_door_id(hass, entry_id, door_id)
        ok = isinstance(res, dict) and res.get("ok") is True

        call_id = getattr(api, "active_call_id", None)
        end_call_result: Any = None
        if ok:
            end_call_result = await _finish_relay_action(hass, entry_id, api)
        else:
            # The call may already be answered (mute-before-open); a failed
            # relay must not leave it hanging without a door behind it.
            end_call_result = await _abort_relay_call(hass, entry_id, api)

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

    async def handle_silence_active_call(call: ServiceCall) -> dict[str, Any]:
        """Answer the active call and end it so the intercom stops ringing.

        Mirrors the APK openDoorSilentlyAndEndCall() ordering minus the relay:
        answer first (200 OK wins the forked call and mutes the panel), then
        terminate the call. No media ever flows through HA.
        """
        requested_entry_id: str | None = call.data.get("config_entry_id")
        entry_id = _select_entry_id(hass, requested_entry_id)
        if not entry_id:
            return {"status": "error", "reason": "no_config_entries"}

        runtime = _entry_runtime(hass, entry_id)
        api = runtime.get(API)
        controller = runtime.get(CALL_CONTROLLER)
        if api is None:
            return {"status": "error", "reason": "api_unavailable", "config_entry_id": entry_id}

        call_id = getattr(api, "active_call_id", None)
        answered: Any = None
        if controller is None or not controller.external_call_established:
            answered = await _answer_panel_call(api)

        if controller is not None:
            end_result = await controller.end_call(source="home_assistant_silence")
        else:
            end_result = await _end_active_call(hass, api)

        ok = isinstance(end_result, dict) and end_result.get("ok") is True
        return {
            "status": "ok" if ok else ("skipped" if end_result is None else "error"),
            "call_id": call_id or None,
            "answered": bool(isinstance(answered, dict) and answered.get("ok") is True),
            "end_call_result": end_result,
            "config_entry_id": entry_id,
        }

    async def handle_reject_active_call(call: ServiceCall) -> dict[str, Any]:
        """End the active call without answering it (603 Decline while ringing).

        Mirrors the APK decline button: endCallSmart() with isCallAccepted
        false rejects our branch. The panel may keep ringing for other
        residents until someone answers or it times out.
        """
        requested_entry_id: str | None = call.data.get("config_entry_id")
        entry_id = _select_entry_id(hass, requested_entry_id)
        if not entry_id:
            return {"status": "error", "reason": "no_config_entries"}

        runtime = _entry_runtime(hass, entry_id)
        api = runtime.get(API)
        controller = runtime.get(CALL_CONTROLLER)
        if api is None:
            return {"status": "error", "reason": "api_unavailable", "config_entry_id": entry_id}

        call_id = getattr(api, "active_call_id", None)
        if controller is not None:
            end_result = await controller.end_call(source="home_assistant_reject")
        else:
            end_result = await _end_active_call(hass, api)

        ok = isinstance(end_result, dict) and end_result.get("ok") is True
        return {
            "status": "ok" if ok else ("skipped" if end_result is None else "error"),
            "call_id": call_id or None,
            "end_call_result": end_result,
            "config_entry_id": entry_id,
        }

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
