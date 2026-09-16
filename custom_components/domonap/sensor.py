from __future__ import annotations


import logging
from datetime import datetime, timezone
from typing import Optional, Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from .runtime_status import runtime_snapshot
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, API, EVENT_INCOMING_CALL
from .util import (
    event_belongs_to_entry,
    extract_phone_digits,
    panel_entity_prefix,
    scoped_entity_unique_id,
)

from .util import scoped_device_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    entities: list[SensorEntity] = []
    api = hass.data[DOMAIN][config_entry.entry_id][API]
    panel_scoped = bool(panel_entity_prefix(config_entry))

    response = await api.get_keys()
    keys = response.get("results", [])

    for key in keys:
        try:
            key_id: str = key["id"]
            door_id: str = key["doorId"]
            door_name: str = key["name"]
            pin: Optional[str] = key.get("domofonPublicPin")

            if not pin:
                _LOGGER.debug(
                    "No domofonPublicPin for door %s (%s), skipping PIN sensor",
                    door_id,
                    door_name,
                )
                continue

            entities.append(
                DomonapDoorCodeSensor(
                    entry_id=config_entry.entry_id,
                    key_id=key_id,
                    door_id=door_id,
                    device_name=door_name,
                    pin=pin,
                    key_data=key,
                    unique_id=scoped_entity_unique_id(
                        config_entry,
                        f"{door_id}_door_code",
                    ),
                )
            )

        except Exception:
            _LOGGER.exception("Failed to create PIN sensor from key payload: %s", key)

    # One per config entry: stores the last DoorId that rang.
    phone_digits = extract_phone_digits(config_entry)
    raw_last_call_unique_id = (
        f"{phone_digits}_last_call_door_id"
        if phone_digits
        else f"{config_entry.entry_id}_last_call_door_id"
    )
    entities.append(
        DomonapLastCallDoorIdSensor(
            hass,
            config_entry.entry_id,
            phone_digits,
            panel_scoped=panel_scoped,
            unique_id=scoped_entity_unique_id(
                config_entry,
                raw_last_call_unique_id,
            ),
        )
    )

    entities.extend(
        DomonapDiagnosticSensor(hass.data[DOMAIN][config_entry.entry_id], config_entry.entry_id, key)
        for key in ("signalr", "sip", "external_sip", "last_event", "last_error")
    )
    async_add_entities(entities, True)


class DomonapDoorCodeSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:key-variant"
    _attr_translation_key = "door_code"
    _attr_should_poll = False

    def __init__(
        self,
        entry_id: str,
        key_id: str,
        door_id: str,
        device_name: str,
        pin: str,
        key_data: dict,
        *,
        unique_id: str,
    ):
        self._entry_id = entry_id
        self._key_id = key_id
        self._door_id = door_id
        self._device_name = device_name
        self._pin = pin
        self._key_data = key_data
        self._unique_id = unique_id

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def native_value(self) -> str | None:
        return self._pin

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._key_data

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, scoped_device_id(self._entry_id, self._key_id))},
            "name": self._device_name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
        }


class DomonapLastCallDoorIdSensor(SensorEntity):
    """Sensor that stores DoorId of the last incoming call."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:phone-incoming"
    _attr_translation_key = "last_call_door_id"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        phone_digits: str | None,
        *,
        panel_scoped: bool,
        unique_id: str,
    ):
        self._hass = hass
        self._entry_id = entry_id
        self._phone_digits = phone_digits
        self._panel_scoped = panel_scoped
        self._unique_id = unique_id
        self._state: str | None = None
        self._attrs: dict[str, Any] = {}
        self._unsub = None

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
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def suggested_object_id(self) -> str | None:
        if self._phone_digits:
            return f"{self._phone_digits}_last_call_door_id"
        return None

    @property
    def native_value(self) -> str | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs

    async def async_added_to_hass(self) -> None:
        self._unsub = self._hass.bus.async_listen(EVENT_INCOMING_CALL, self._handle_incoming_call)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_incoming_call(self, event) -> None:
        if not event_belongs_to_entry(
            event.data,
            self._entry_id,
            panel_scoped=self._panel_scoped,
        ):
            return

        door_id = event.data.get("DoorId")
        if not door_id:
            return

        self._state = str(door_id)
        attrs = dict(event.data)
        attrs["ts"] = datetime.now(timezone.utc).isoformat() + "Z"

        self._attrs = attrs
        self.async_write_ha_state()


class DomonapDiagnosticSensor(SensorEntity):
    """Poll local state only; never initiate diagnostic network requests."""
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, runtime, entry_id, key):
        self._runtime = runtime
        self._entry_id = entry_id
        self._key = key
        self._attr_unique_id = f"{entry_id}_diagnostic_{key}"
        self._attr_translation_key = key
        if key == "last_event":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif key in ("signalr", "sip", "external_sip"):
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = (
                ["connected", "disconnected"] if key == "signalr"
                else ["idle", "registered", "unregistered", "disabled"]
            )

    @property
    def native_value(self):
        return runtime_snapshot(self._runtime).get(self._key)

    @property
    def extra_state_attributes(self):
        if self._key == "last_error":
            timestamp = runtime_snapshot(self._runtime).get("last_error_at")
            return {"last_error_at": timestamp.isoformat() if timestamp else None}
        return {}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, scoped_device_id(self._entry_id, "diagnostics"))},
            "name": "Domonap connection", "manufacturer": "Domonap",
            "model": "Connection diagnostics",
        }
