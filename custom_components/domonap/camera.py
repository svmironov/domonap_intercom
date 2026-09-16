import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    StreamType,
)
from homeassistant.core import callback

try:
    from homeassistant.components.camera import (
        WebRTCAnswer,
        WebRTCClientConfiguration,
        WebRTCError,
        WebRTCSendMessage,
    )
except ImportError:
    WebRTCAnswer = None
    WebRTCClientConfiguration = None
    WebRTCError = None
    WebRTCSendMessage = None

from .const import API, DOMAIN, PARAM_WEBRTC_PROXY_SECRET, WEBRTC_PROXY
from .util import scoped_entity_unique_id
from .webrtc_proxy import _resolve_upstream_session_url

_LOGGER = logging.getLogger(__name__)
CAMERA_CATEGORY_NAMES = {
    "Parking": "Parking",
    "House": "House",
    "Entrance": "Entrance",
    "None": "None",
}


@dataclass(frozen=True)
class WHEPMedia:
    media: str
    mid: str


@dataclass(frozen=True)
class WHEPOfferData:
    ice_ufrag: str
    ice_pwd: str
    medias: list[WHEPMedia]


@dataclass(frozen=True)
class WHEPSession:
    session_url: str
    offer_data: WHEPOfferData


async def async_setup_entry(hass, config_entry, async_add_entities):
    api = hass.data[DOMAIN][config_entry.entry_id][API]
    proxy = hass.data[DOMAIN][WEBRTC_PROXY]
    proxy_secret = config_entry.data.get(PARAM_WEBRTC_PROXY_SECRET)
    key_response = await api.get_paged_keys()
    key_entities = _build_key_camera_entities(
        config_entry,
        api,
        proxy,
        proxy_secret,
        key_response,
    )
    if key_entities:
        async_add_entities(key_entities, True)

    video_areas_response = await api.get_video_area()
    video_entities = await _build_video_camera_entities(
        config_entry,
        api,
        proxy,
        proxy_secret,
        video_areas_response,
    )
    if video_entities:
        async_add_entities(video_entities, True)

    return True


def _build_key_camera_entities(
    config_entry,
    api,
    proxy,
    proxy_secret: str | None,
    response,
) -> list[Camera]:
    if isinstance(response, Exception):
        _LOGGER.exception("Failed to load Domonap key cameras", exc_info=response)
        return []

    if not isinstance(response, dict):
        _LOGGER.warning(
            "Unexpected Domonap key camera payload: %s", type(response).__name__
        )
        return []

    if "error" in response:
        _log_api_error("Loading Domonap key cameras", response)
        return []

    entities: list[Camera] = []
    for key in response.get("results", []):
        key_id = key.get("id")
        name = key.get("name")
        if not key_id or not name:
            _LOGGER.debug("Skipping invalid key camera payload: %s", key)
            continue

        if not (key.get("httpVideoUrl") or key.get("webrtcVideoUrl")):
            continue

        camera_class = (
            IntercomWebRTCCamera if key.get("webrtcVideoUrl") else IntercomCamera
        )
        entities.append(
            camera_class(
                api,
                key_id,
                name,
                key.get("httpVideoUrl"),
                key.get("videoPreview"),
                key,
                proxy=proxy,
                proxy_secret=proxy_secret,
                entity_unique_id=scoped_entity_unique_id(
                    config_entry,
                    str(key_id),
                ),
            )
        )

    return entities


async def _build_video_camera_entities(
    config_entry,
    api,
    proxy,
    proxy_secret: str | None,
    response,
) -> list[Camera]:
    if isinstance(response, Exception):
        _LOGGER.exception("Failed to load Domonap video areas", exc_info=response)
        return []

    if isinstance(response, dict) and "error" in response:
        _log_api_error("Loading Domonap video areas", response)
        return []

    if not isinstance(response, list):
        if response is not None:
            _LOGGER.warning(
                "Unexpected Domonap video areas payload: %s", type(response).__name__
            )
        return []

    categories: list[str] = []
    for area in response:
        if not isinstance(area, dict):
            continue
        category = area.get("category")
        if category and category not in categories:
            categories.append(category)

    if not categories:
        return []

    category_responses = await asyncio.gather(
        *(api.get_user_video_cameras(category) for category in categories),
        return_exceptions=True,
    )

    entities: list[Camera] = []
    seen_unique_ids: set[str] = set()

    for category, category_response in zip(categories, category_responses):
        if isinstance(category_response, Exception):
            _LOGGER.exception(
                "Failed to load Domonap cameras for category %s",
                category,
                exc_info=category_response,
            )
            continue

        if isinstance(category_response, dict) and "error" in category_response:
            _log_api_error(
                f"Loading Domonap cameras for category {category}", category_response
            )
            continue

        if not isinstance(category_response, list):
            _LOGGER.warning(
                "Unexpected Domonap cameras payload for category %s: %s",
                category,
                type(category_response).__name__,
            )
            continue

        category_name = CAMERA_CATEGORY_NAMES.get(category, category)
        for camera in category_response:
            entity = _make_video_camera_entity(
                config_entry,
                api,
                proxy,
                proxy_secret,
                camera,
                category,
                category_name,
            )
            if entity is None or entity.unique_id in seen_unique_ids:
                continue

            seen_unique_ids.add(entity.unique_id)
            entities.append(entity)

    return entities


def _make_video_camera_entity(
    config_entry,
    api,
    proxy,
    proxy_secret: str | None,
    camera: dict,
    category: str,
    category_name: str,
):
    if not isinstance(camera, dict):
        return None

    camera_id = camera.get("id")
    name = camera.get("name")
    if not camera_id or not name:
        _LOGGER.debug("Skipping invalid Domonap video camera payload: %s", camera)
        return None

    if not (camera.get("httpVideoUrl") or camera.get("webrtcVideoUrl")):
        return None

    entity_unique_id = f"video_camera_{camera_id}"
    camera_data = dict(camera)
    camera_data["category"] = category
    camera_data["categoryName"] = category_name
    camera_data["source"] = "video_tab"

    camera_class = (
        IntercomWebRTCCamera if camera.get("webrtcVideoUrl") else IntercomCamera
    )
    return camera_class(
        api,
        entity_unique_id,
        name,
        camera.get("httpVideoUrl"),
        camera.get("videoPreviewUrl"),
        camera_data,
        proxy=proxy,
        proxy_secret=proxy_secret,
        entity_unique_id=scoped_entity_unique_id(
            config_entry,
            entity_unique_id,
        ),
        device_identifier=entity_unique_id,
        device_name=name,
        device_model="Video Camera",
    )


def _log_api_error(context: str, response: dict) -> None:
    _LOGGER.warning(
        "%s failed: %s %s",
        context,
        response.get("error"),
        str(response.get("body", ""))[:200],
    )


class IntercomCamera(Camera):
    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_frontend_stream_type = StreamType.HLS
    _attr_motion_detection_enabled = False
    _attr_translation_key = "camera"

    def __init__(
        self,
        api,
        key_id: str,
        name: str,
        stream_url: str | None,
        snapshot_url: str | None,
        key_data: dict,
        proxy=None,
        proxy_secret: str | None = None,
        *,
        entity_unique_id: str | None = None,
        device_identifier: str | None = None,
        device_name: str | None = None,
        device_model: str = "Intercom Device",
    ):
        super().__init__()
        self._api = api
        self._key_id = key_id
        self._entity_unique_id = entity_unique_id or key_id
        self._name = name
        self._stream_url = stream_url
        self._snapshot_url = snapshot_url
        self._key_data = key_data
        self._proxy = proxy
        self._proxy_secret = proxy_secret
        self._proxy_stream_path: str | None = None
        self._proxy_stream_url: str | None = None
        self._device_identifier = device_identifier or key_id
        self._device_name = device_name or name
        self._device_model = device_model

    @property
    def extra_state_attributes(self):
        attributes = dict(self._key_data)
        if self._proxy_stream_path:
            attributes["go2rtc_webrtc_path"] = self._proxy_stream_path
        if self._proxy_stream_url:
            attributes["go2rtc_webrtc_url"] = self._proxy_stream_url
        return attributes

    @property
    def unique_id(self):
        return self._entity_unique_id

    async def async_camera_image(self, width=None, height=None):
        if not self._snapshot_url:
            return None

        response = await self._api.fetch_external_bytes(self._snapshot_url)
        if response["ok"]:
            _LOGGER.debug(f"Successfully fetched snapshot for {self._name}")
            return response["body"]

        _LOGGER.error(
            "Failed to fetch snapshot from %s: %s",
            self._snapshot_url,
            response.get("error"),
        )
        return None

    async def stream_source(self):
        return self._stream_url or None

    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_identifier)},
            "name": self._device_name,
            "manufacturer": "Domonap",
            "model": self._device_model,
        }

    async def async_update(self):
        _LOGGER.debug(f"Updating camera: {self._name}")


class IntercomWebRTCCamera(IntercomCamera):
    _attr_frontend_stream_type = getattr(StreamType, "WEB_RTC", StreamType.HLS)

    def __init__(
        self,
        api,
        key_id: str,
        name: str,
        stream_url: str | None,
        snapshot_url: str | None,
        key_data: dict,
        **kwargs,
    ):
        super().__init__(
            api,
            key_id,
            name,
            stream_url,
            snapshot_url,
            key_data,
            **kwargs,
        )
        self._webrtc_url = key_data["webrtcVideoUrl"]
        self._whep_url = _whep_url_from_webrtc_url(self._webrtc_url)
        self._webrtc_sessions: dict[str, WHEPSession] = {}
        self._pending_candidates = defaultdict(list)
        if self._proxy and self._proxy_secret:
            self._proxy_stream_path = self._proxy.register_camera(
                self._proxy_secret,
                self.unique_id,
                self._api,
                self._whep_url,
            )
            self._proxy_stream_url = self._proxy.get_proxy_url(
                self._proxy_secret,
                self.unique_id,
            )

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        if WebRTCAnswer is None or WebRTCError is None:
            _LOGGER.error("Home Assistant WebRTC API is not available")
            return

        offer_data = _parse_offer_sdp(offer_sdp)
        response = await self._api.create_whep_session(self._whep_url, offer_sdp)
        if not response["ok"]:
            send_message(
                WebRTCError(
                    "domonap_webrtc_offer_failed",
                    f"Domonap WHEP offer failed: {response.get('error')}",
                )
            )
            return

        self._webrtc_sessions[session_id] = WHEPSession(
            session_url=_resolve_upstream_session_url(
                self._whep_url,
                response["location"],
            ),
            offer_data=offer_data,
        )
        send_message(WebRTCAnswer(response["answer_sdp"]))

        pending_candidates = self._pending_candidates.pop(session_id, [])
        if pending_candidates:
            await self._async_send_webrtc_candidates(session_id, pending_candidates)

    @callback
    def _async_get_webrtc_client_configuration(self):
        if WebRTCClientConfiguration is None:
            return super()._async_get_webrtc_client_configuration()

        return WebRTCClientConfiguration(data_channel="domonap")

    async def async_on_webrtc_candidate(self, session_id: str, candidate) -> None:
        if not getattr(candidate, "candidate", None):
            return

        if session_id not in self._webrtc_sessions:
            self._pending_candidates[session_id].append(candidate)
            return

        await self._async_send_webrtc_candidates(session_id, [candidate])

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        self._pending_candidates.pop(session_id, None)
        whep_session = self._webrtc_sessions.pop(session_id, None)
        if whep_session is None:
            return

        self.hass.async_create_task(
            self._async_close_whep_session(whep_session.session_url)
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._proxy and self._proxy_secret:
            self._proxy.unregister_camera(self._proxy_secret, self.unique_id)

    async def _async_send_webrtc_candidates(self, session_id: str, candidates) -> None:
        whep_session = self._webrtc_sessions.get(session_id)
        if whep_session is None:
            return

        sdp_fragment = _generate_sdp_fragment(whep_session.offer_data, candidates)
        if not sdp_fragment:
            return

        response = await self._api.send_whep_candidates(
            whep_session.session_url,
            sdp_fragment,
        )
        if not response["ok"]:
            _LOGGER.warning(
                "Domonap WHEP candidate failed for %s: %s %s",
                self._name,
                response.get("error"),
                response.get("body", "")[:200],
            )

    async def _async_close_whep_session(self, session_url: str) -> None:
        response = await self._api.close_whep_session(session_url)
        if not response["ok"]:
            _LOGGER.debug(
                "Domonap WHEP session close failed for %s: %s",
                self._name,
                response.get("error"),
            )


def _parse_offer_sdp(offer_sdp: str) -> WHEPOfferData:
    ice_ufrag = ""
    ice_pwd = ""
    medias: list[WHEPMedia] = []

    for line in offer_sdp.split("\r\n"):
        if line.startswith("m="):
            medias.append(WHEPMedia(media=line[2:], mid=str(len(medias))))
        elif line.startswith("a=mid:") and medias:
            medias[-1] = WHEPMedia(media=medias[-1].media, mid=line[6:])
        elif not ice_ufrag and line.startswith("a=ice-ufrag:"):
            ice_ufrag = line[len("a=ice-ufrag:"):]
        elif not ice_pwd and line.startswith("a=ice-pwd:"):
            ice_pwd = line[len("a=ice-pwd:"):]

    return WHEPOfferData(ice_ufrag=ice_ufrag, ice_pwd=ice_pwd, medias=medias)


def _whep_url_from_webrtc_url(webrtc_url: str) -> str:
    parsed = urlsplit(webrtc_url)
    path = parsed.path.rstrip("/") + "/whep"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _generate_sdp_fragment(offer_data: WHEPOfferData, candidates) -> str | None:
    candidates_by_media = defaultdict(list)

    for candidate in candidates:
        candidate_value = getattr(candidate, "candidate", None)
        if not candidate_value:
            continue

        media_index = _candidate_media_index(offer_data, candidate)
        if media_index is None:
            continue
        if media_index < 0 or media_index >= len(offer_data.medias):
            continue

        candidates_by_media[media_index].append(candidate_value)

    if not candidates_by_media:
        return None

    fragment = f"a=ice-ufrag:{offer_data.ice_ufrag}\r\n"
    if offer_data.ice_pwd:
        fragment += f"a=ice-pwd:{offer_data.ice_pwd}\r\n"

    for media_index, media in enumerate(offer_data.medias):
        if media_index not in candidates_by_media:
            continue

        fragment += f"m={media.media}\r\n"
        fragment += f"a=mid:{media.mid}\r\n"
        for candidate_value in candidates_by_media[media_index]:
            fragment += f"a={candidate_value}\r\n"

    return fragment


def _candidate_media_index(offer_data: WHEPOfferData, candidate) -> int | None:
    media_index = getattr(candidate, "sdp_m_line_index", None)
    if media_index is not None:
        return media_index

    candidate_mid = getattr(candidate, "sdp_mid", None)
    if candidate_mid is None:
        return 0 if offer_data.medias else None

    for index, media in enumerate(offer_data.medias):
        if media.mid == candidate_mid:
            return index

    if str(candidate_mid).isdigit():
        return int(candidate_mid)

    return None