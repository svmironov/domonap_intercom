import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, API, CALL_CONTROLLER
from .util import extract_phone_digits, scoped_entity_unique_id, find_last_call_sensor_entity_id
from .relay import open_relay, raise_for_relay_error

from .util import scoped_device_id

_LOGGER = logging.getLogger(__name__)


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
    response = await api.get_keys()
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
            "identifiers": {(DOMAIN, scoped_device_id(self._entry_id, phone))},
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
        sensor_entity_id = find_last_call_sensor_entity_id(self.hass, self._entry_id)
        state = self.hass.states.get(sensor_entity_id) if sensor_entity_id else None
        if state is None or state.state in ("unknown", "unavailable", "none", "None", ""):
            raise HomeAssistantError("No last call is available for this Domonap account")
        result = await open_relay(
            self.hass, self._api, self._controller, state.state,
            by_door=True, source="home_assistant_button",
        )
        raise_for_relay_error(result)


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
            "identifiers": {(DOMAIN, scoped_device_id(self._api.config_entry_id, self._key_id))},
            "name": self._name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
        }

    async def async_press(self):
        result = await open_relay(
            self.hass, self._api, self._controller, self._key_id,
            by_door=False, source="home_assistant_button",
        )
        raise_for_relay_error(result)
