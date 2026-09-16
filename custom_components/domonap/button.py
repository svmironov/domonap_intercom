import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, API, CALL_CONTROLLER, EVENT_CALL_ENDED
from .util import extract_phone_digits, scoped_entity_unique_id

_LOGGER = logging.getLogger(__name__)


async def _end_active_call(hass: HomeAssistant, api) -> None:
    """Best-effort end of a call after a door was opened."""
    call_id = getattr(api, "active_call_id", None)
    if not call_id:
        return

    try:
        result = await api.end_active_call()
        if result is not None and not (isinstance(result, dict) and result.get("ok") is True):
            _LOGGER.error(
                "Failed to end active call %s after opening the door: %s",
                call_id,
                result,
            )
        elif isinstance(result, dict) and result.get("ok") is True:
            _LOGGER.info("Active call %s ended after opening the door", call_id)
            hass.bus.fire(EVENT_CALL_ENDED, {"CallId": call_id})
    except Exception:
        _LOGGER.exception("Failed to end active call %s after opening the door", call_id)


async def _finish_after_relay(hass: HomeAssistant, api, controller) -> None:
    if controller is not None:
        result = await controller.end_after_relay(source="home_assistant_button")
        if isinstance(result, dict) and not result.get("ok", False):
            _LOGGER.warning("Panel call teardown after button relay was incomplete: %s", result)
        return
    await _end_active_call(hass, api)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    entities: list[ButtonEntity] = []

    runtime = hass.data[DOMAIN][config_entry.entry_id]
    api = runtime[API]
    controller = runtime.get(CALL_CONTROLLER)

    # Button: open relay using door_id from the last incoming call
    phone_digits = extract_phone_digits(config_entry) or config_entry.entry_id
    last_call_raw_unique_id = f"{phone_digits}_last_call_door_id"
    entities.append(
        IntercomOpenLastCallDoor(
            api,
            controller,
            config_entry.entry_id,
            phone_digits,
            unique_id=scoped_entity_unique_id(
                config_entry,
                f"{phone_digits}_open_relay_by_last_call_door_id",
            ),
            last_call_sensor_unique_id=scoped_entity_unique_id(
                config_entry,
                last_call_raw_unique_id,
            ),
        )
    )

    # Existing per-door buttons
    response = await api.get_paged_keys()
    keys = response.get("results", [])
    for key in keys:
        key_id = key["id"]
        door_id = key["doorId"]
        door_name = key["name"]
        entities.append(
            IntercomDoor(
                api,
                controller,
                key_id,
                door_id,
                door_name,
                key,
                unique_id=scoped_entity_unique_id(config_entry, str(door_id)),
            )
        )

    async_add_entities(entities, True)


class IntercomOpenLastCallDoor(ButtonEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:phone-incoming"
    _attr_translation_key = "open_relay_by_last_call_door_id"

    def __init__(
        self,
        api,
        controller,
        entry_id: str,
        phone_digits: str,
        *,
        unique_id: str,
        last_call_sensor_unique_id: str,
    ):
        self._api = api
        self._controller = controller
        self._entry_id = entry_id
        self._phone_digits = phone_digits
        self._unique_id = unique_id
        self._last_call_sensor_unique_id = last_call_sensor_unique_id

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def device_info(self):
        phone = self._phone_digits or self._entry_id
        return {
            "identifiers": {(DOMAIN, phone)},
            "name": f"Domonap {phone}",
            "manufacturer": "Domonap",
            "model": "Domonap Account",
        }

    @property
    def suggested_object_id(self) -> str:
        # Keep readable/stable entity_id suggestions; registry unique_id carries
        # the Panel account namespace separately.
        return f"{self._phone_digits}_open_relay_by_last_call_door_id"

    async def async_press(self) -> None:
        registry = er.async_get(self.hass)
        sensor_entity_id = registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            self._last_call_sensor_unique_id,
        )
        if sensor_entity_id is None:
            # Backward-compatible fallback for a registry that has not yet been
            # migrated during the current startup.
            sensor_entity_id = f"sensor.{self._phone_digits}_last_call_door_id"

        state = self.hass.states.get(sensor_entity_id) if self.hass else None
        if state is None or state.state in ("unknown", "unavailable", "none", "None", ""):
            _LOGGER.debug("No last call door_id found in %s", sensor_entity_id)
            return

        door_id = state.state
        try:
            if self._controller is not None:
                res = await self._controller.open_door_by_door_id(door_id)
            else:
                res = await self._api.open_relay_by_door_id(door_id)
            if not (isinstance(res, dict) and res.get("ok") is True):
                _LOGGER.error("Failed to open relay by last call door_id=%s: %s", door_id, res)
                # The call may already be answered (mute-before-open); a failed
                # relay must not leave it hanging without a door behind it.
                if self._controller is not None:
                    await self._controller.end_call(source="relay_open_failed")
                else:
                    await _end_active_call(self.hass, self._api)
                return

            await _finish_after_relay(self.hass, self._api, self._controller)

        except Exception:
            _LOGGER.exception("Error opening relay by last call door_id=%s", door_id)


class IntercomDoor(ButtonEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:lock"
    _attr_translation_key = "open_door"

    def __init__(
        self,
        api,
        controller,
        key_id,
        door_id: str,
        name: str,
        key_data: dict,
        *,
        unique_id: str,
    ):
        self._api = api
        self._controller = controller
        self._key_id = key_id
        self._door_id = door_id
        self._name = name
        self._key_data = key_data
        self._unique_id = unique_id

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._key_data

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._key_id)},
            "name": self._name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
        }

    async def async_press(self):
        try:
            if self._controller is not None:
                response = await self._controller.open_door_by_key_id(self._key_id)
            else:
                response = await self._api.open_relay_by_key_id(self._key_id)
            if response.get("ok") is not True:
                _LOGGER.error("Failed to open the door %s. Response: %s", self._name, response)
                # The call may already be answered (mute-before-open); a failed
                # relay must not leave it hanging without a door behind it.
                if self._controller is not None:
                    await self._controller.end_call(source="relay_open_failed")
                else:
                    await _end_active_call(self.hass, self._api)
                return
            await _finish_after_relay(self.hass, self._api, self._controller)
        except Exception as e:
            _LOGGER.error("Error opening the door %s: %s", self._name, e)
