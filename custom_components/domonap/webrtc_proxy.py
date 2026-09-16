from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin
from uuid import uuid4

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # pragma: no cover - depends on HA version
    NoURLAvailableError = ValueError
    get_url = None


@dataclass(slots=True)
class WebRTCProxyTarget:
    api: object
    camera_id: str
    whep_url: str


@dataclass(slots=True)
class WebRTCProxySession:
    api: object
    upstream_session_url: str


class DomonapWebRTCProxy:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._targets: dict[tuple[str, str], WebRTCProxyTarget] = {}
        self._sessions: dict[str, WebRTCProxySession] = {}
        self._offer_tasks: dict[asyncio.Task, object] = {}

    def register_camera(
        self,
        proxy_secret: str,
        camera_id: str,
        api: object,
        whep_url: str,
    ) -> str:
        self._targets[(proxy_secret, camera_id)] = WebRTCProxyTarget(
            api=api,
            camera_id=camera_id,
            whep_url=whep_url,
        )
        return self.get_proxy_path(proxy_secret, camera_id)

    def unregister_camera(self, proxy_secret: str, camera_id: str) -> None:
        self._targets.pop((proxy_secret, camera_id), None)

    async def close_for_api(self, api) -> None:
        """Remove targets first so in-flight offers cannot leave new sessions."""
        for key, target in list(self._targets.items()):
            if target.api is api:
                self._targets.pop(key, None)
        await asyncio.gather(
            *(task for task, owner in list(self._offer_tasks.items()) if owner is api),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(self.delete_session(key) for key, session in list(self._sessions.items())
              if session.api is api),
            return_exceptions=True,
        )

    def get_proxy_path(self, proxy_secret: str, camera_id: str) -> str:
        return f"/api/{DOMAIN}/webrtc_proxy/{proxy_secret}/{camera_id}/whep"

    def get_proxy_url(self, proxy_secret: str, camera_id: str) -> str | None:
        path = self.get_proxy_path(proxy_secret, camera_id)
        if get_url is None:
            return None

        try:
            return f"{get_url(self._hass, prefer_external=False)}{path}"
        except (NoURLAvailableError, TypeError, ValueError):
            try:
                return f"{get_url(self._hass, prefer_external=True)}{path}"
            except (NoURLAvailableError, TypeError, ValueError):
                _LOGGER.debug("Unable to build Domonap WebRTC proxy URL", exc_info=True)
                return None

    async def create_session(
        self,
        proxy_secret: str,
        camera_id: str,
        offer_sdp: str,
    ) -> tuple[int, dict[str, str], str]:
        target = self._targets.get((proxy_secret, camera_id))
        if target is None:
            raise web.HTTPNotFound(text="Unknown camera stream")

        task = asyncio.current_task()
        self._offer_tasks[task] = target.api
        try:
            return await self._create_session(proxy_secret, camera_id, offer_sdp, target)
        finally:
            self._offer_tasks.pop(task, None)

    async def _create_session(self, proxy_secret, camera_id, offer_sdp, target):
        response = await target.api.create_whep_session(target.whep_url, offer_sdp)
        if not response.get("ok"):
            _LOGGER.warning(
                "Domonap WebRTC proxy offer failed for %s: %s",
                camera_id,
                response.get("error"),
            )
            raise web.HTTPBadGateway(text=str(response.get("error", "WHEP offer failed")))

        if self._targets.get((proxy_secret, camera_id)) is not target:
            await target.api.close_whep_session(
                _resolve_upstream_session_url(target.whep_url, response["location"])
            )
            raise web.HTTPNotFound(text="Camera stream was removed")

        session_id = uuid4().hex
        session_url = self._session_url(session_id)
        self._sessions[session_id] = WebRTCProxySession(
            api=target.api,
            upstream_session_url=_resolve_upstream_session_url(
                target.whep_url,
                response["location"],
            ),
        )
        headers = {
            "Content-Type": "application/sdp",
            "Location": session_url,
        }
        return 201, headers, response["answer_sdp"]

    async def patch_session(self, session_id: str, sdp_fragment: str) -> web.Response:
        session = self._sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(text="Unknown WebRTC session")

        response = await session.api.send_whep_candidates(
            session.upstream_session_url,
            sdp_fragment,
        )
        if not response.get("ok"):
            raise web.HTTPBadGateway(
                text=str(response.get("error", "WHEP candidate patch failed"))
            )

        return web.Response(status=response.get("status", 204))

    async def delete_session(self, session_id: str) -> web.Response:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return web.Response(status=204)

        response = await session.api.close_whep_session(session.upstream_session_url)
        if not response.get("ok"):
            _LOGGER.debug(
                "Domonap WebRTC proxy close failed for %s: %s",
                session_id,
                response.get("error"),
            )
            return web.Response(status=502, text=str(response.get("error", "")))

        return web.Response(status=response.get("status", 204))

    def _session_url(self, session_id: str) -> str:
        return f"/api/{DOMAIN}/webrtc_proxy/session/{session_id}"


def _resolve_upstream_session_url(whep_url: str, location: str) -> str:
    base_url = whep_url if whep_url.endswith("/") else f"{whep_url}/"
    return urljoin(base_url, location)


class DomonapWebRTCProxyView(HomeAssistantView):
    url = f"/api/{DOMAIN}/webrtc_proxy/{{proxy_secret}}/{{camera_id}}/whep"
    name = f"api:{DOMAIN}:webrtc_proxy_offer"
    requires_auth = False

    def __init__(self, proxy: DomonapWebRTCProxy) -> None:
        self._proxy = proxy

    async def post(self, request: web.Request, proxy_secret: str, camera_id: str) -> web.Response:
        offer_sdp = await request.text()
        status, headers, body = await self._proxy.create_session(
            proxy_secret,
            camera_id,
            offer_sdp,
        )
        if headers.get("Location", "").startswith("/"):
            headers["Location"] = str(
                request.url.with_path(headers["Location"]).with_query({})
            )
        return web.Response(status=status, headers=headers, text=body)


class DomonapWebRTCProxySessionView(HomeAssistantView):
    url = f"/api/{DOMAIN}/webrtc_proxy/session/{{session_id}}"
    name = f"api:{DOMAIN}:webrtc_proxy_session"
    requires_auth = False

    def __init__(self, proxy: DomonapWebRTCProxy) -> None:
        self._proxy = proxy

    async def patch(self, request: web.Request, session_id: str) -> web.Response:
        return await self._proxy.patch_session(session_id, await request.text())

    async def delete(self, request: web.Request, session_id: str) -> web.Response:
        return await self._proxy.delete_session(session_id)
