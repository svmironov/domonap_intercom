from __future__ import annotations


import asyncio
import logging
from typing import Optional, Callable

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .const import DOMAIN, API, EVENT_INCOMING_CALL
from .util import event_belongs_to_entry, panel_entity_prefix, scoped_entity_unique_id

from .util import scoped_device_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    entities: list[IntercomCallImageEntity] = []
    api = hass.data[DOMAIN][config_entry.entry_id][API]
    panel_scoped = bool(panel_entity_prefix(config_entry))

    response = await api.get_keys()
    keys = response.get("results", [])

    for key in keys:
        if key.get("videoPreview") is not None:
            key_id: str = key["id"]
            door_id: str = key["doorId"]
            door_name: str = key["name"]
            photo_url: str = key["videoPreview"]

            entities.append(
                IntercomCallImageEntity(
                    hass=hass,
                    api=api,
                    key_id=key_id,
                    door_id=door_id,
                    device_name=door_name,
                    photo_url=photo_url,
                    key_data=key,
                    entry_id=config_entry.entry_id,
                    panel_scoped=panel_scoped,
                    unique_id=scoped_entity_unique_id(
                        config_entry,
                        f"{door_id}_photo",
                    ),
                )
            )

    async_add_entities(entities, True)


class IntercomCallImageEntity(ImageEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "incoming_call_image"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        api,
        key_id: str,
        door_id: str,
        device_name: str,
        photo_url: Optional[str] = None,
        key_data: dict = None,
        *,
        entry_id: str,
        panel_scoped: bool,
        unique_id: str,
    ):
        super().__init__(hass, verify_ssl=True)
        self._api = api
        self._key_id = key_id
        self._door_id = door_id
        self._device_name = device_name
        self._photo_url = photo_url
        self._key_data = key_data
        self._entry_id = entry_id
        self._panel_scoped = panel_scoped
        self._unique_id = unique_id
        self._image_bytes: Optional[bytes] = None
        self._unsub: Optional[Callable[[], None]] = None
        self._fetch_task: asyncio.Task | None = None
        self._generation = 0
        self._removed = False

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._key_data

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, scoped_device_id(self._api.config_entry_id, self._key_id))},
            "name": self._device_name,
            "manufacturer": "Domonap",
            "model": "Intercom Device",
        }

    async def async_added_to_hass(self) -> None:
        self._unsub = self.hass.bus.async_listen(
            EVENT_INCOMING_CALL, self._handle_incoming_call
        )

        if self._photo_url:
            self._start_fetch(self._photo_url)

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._generation += 1
        if self._fetch_task is not None:
            self._fetch_task.cancel()
            await asyncio.gather(self._fetch_task, return_exceptions=True)
            self._fetch_task = None
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def async_image(self) -> bytes | None:
        return self._image_bytes

    @callback
    def _handle_incoming_call(self, event) -> None:
        if not event_belongs_to_entry(
            event.data,
            self._entry_id,
            panel_scoped=self._panel_scoped,
        ):
            return
        if event.data.get("DoorId") != self._door_id:
            return

        original_photo_url: Optional[str] = event.data.get("OriginalPhotoUrl")
        original_video_preview: Optional[str] = event.data.get("OriginalVideoPreview")
        photo_url: Optional[str] = (
            original_photo_url
            or original_video_preview
            or event.data.get("PhotoUrl")
        )
        if not photo_url:
            return
        authorized = original_photo_url is None
        fallback_url: Optional[str] = original_video_preview or event.data.get(
            "VideoPreview"
        )

        self._start_fetch(photo_url, authorized=authorized, fallback_url=fallback_url)

    def _start_fetch(self, photo_url, **kwargs):
        if self._removed:
            return
        self._generation += 1
        generation = self._generation
        previous = self._fetch_task
        if previous is not None:
            previous.cancel()

        async def fetch_and_set():
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            try:
                data = await self._http_get_bytes(photo_url, **kwargs)
                if data and not self._removed and generation == self._generation:
                    await self._set_image(data)
            except Exception:
                _LOGGER.warning("Cannot load Domonap call image", exc_info=True)

        self._fetch_task = self.hass.async_create_task(fetch_and_set())

    async def _set_image(self, data: bytes) -> None:
        self._image_bytes = data
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def _http_get_bytes(
        self,
        url: str,
        *,
        authorized: bool = True,
        fallback_url: Optional[str] = None,
        fallback_authorized: bool = True,
    ) -> Optional[bytes]:
        response = await self._api.fetch_external_bytes(url, authorized=authorized)
        if response.get("ok"):
            return response["body"]

        if fallback_url:
            _LOGGER.debug(
                "GET %s failed: %s. Trying fallback %s",
                url,
                response.get("error"),
                fallback_url,
            )
            response = await self._api.fetch_external_bytes(
                fallback_url,
                authorized=fallback_authorized,
            )
            if response.get("ok"):
                return response["body"]

        _LOGGER.debug("GET %s failed: %s", url, response.get("error"))
        return None
