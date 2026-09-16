"""Shared relay and call cleanup policy for services and entity buttons."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import EVENT_CALL_ENDED

_LOGGER = logging.getLogger(__name__)


async def end_active_call(hass, api, controller=None, *, source="manual", expected_call_id=None):
    """Report teardown failures without masking the result of a relay request."""
    call_id = expected_call_id or getattr(api, "active_call_id", None)
    try:
        if controller is not None:
            return await controller.end_call(source=source, expected_call_id=expected_call_id)
        if not call_id:
            return None
        result = await api.end_active_call(expected_call_id=call_id)
        if isinstance(result, dict) and result.get("ok") is True and not result.get("skipped"):
            event = {"CallId": call_id}
            if getattr(api, "config_entry_id", None):
                event["config_entry_id"] = api.config_entry_id
            hass.bus.fire(EVENT_CALL_ENDED, event)
        return result
    except Exception:
        _LOGGER.exception("Call teardown failed after %s", source)
        return {"ok": False, "error": "call_teardown_failed"}


async def open_relay(hass, api, controller, relay_id: str, *, by_door: bool, source: str) -> dict[str, Any]:
    """Open once, then clean up even if the network request raises or is cancelled."""
    call_id = getattr(api, "active_call_id", None) or getattr(controller, "_active_call_id", None)
    result = None
    end_result = None
    try:
        if controller is not None:
            method = controller.open_door_by_door_id if by_door else controller.open_door_by_key_id
        else:
            method = api.open_relay_by_door_id if by_door else api.open_relay_by_key_id
        result = await method(relay_id)
    except Exception as err:
        raise HomeAssistantError("Domonap door opening failed; check the connection") from err
    finally:
        # Never end a replacement call that arrived during this request.
        if call_id:
            ok = isinstance(result, dict) and result.get("ok") is True
            end_result = await end_active_call(
                hass, api, controller,
                source=source if ok else "relay_open_failed",
                expected_call_id=call_id,
            )
    return {"response": result, "end_call_result": end_result, "call_id": call_id}


def raise_for_relay_error(result: dict[str, Any]) -> None:
    response = result["response"]
    if not (isinstance(response, dict) and response.get("ok") is True):
        raise HomeAssistantError("Domonap could not open the door")
