from __future__ import annotations

import logging
from dataclasses import dataclass
from secrets import token_urlsafe

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
class MediaProxyTarget:
    api: object
    url: str
    fallback_url: str | None = None
    authorized: bool = True
    fallback_authorized: bool = True


class DomonapMediaProxy:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._targets: dict[tuple[str, str], MediaProxyTarget] = {}

    def register_url(
        self,
        proxy_secret: str,
        api: object,
        url: str,
        fallback_url: str | None = None,
        authorized: bool = True,
        fallback_authorized: bool = True,
    ) -> str:
        token = token_urlsafe(18)
        self._targets[(proxy_secret, token)] = MediaProxyTarget(
            api=api,
            url=url,
            fallback_url=fallback_url,
            authorized=authorized,
            fallback_authorized=fallback_authorized,
        )
        return self.get_proxy_url(proxy_secret, token)

    def unregister_api(self, api) -> None:
        for key, target in list(self._targets.items()):
            if target.api is api:
                self._targets.pop(key, None)

    def get_proxy_path(self, proxy_secret: str, token: str) -> str:
        return f"/api/{DOMAIN}/media_proxy/{proxy_secret}/{token}"

    def get_proxy_url(self, proxy_secret: str, token: str) -> str:
        path = self.get_proxy_path(proxy_secret, token)
        base_url = self._get_base_url()
        return f"{base_url}{path}" if base_url else path

    async def get_media(self, proxy_secret: str, token: str) -> web.Response:
        target = self._targets.get((proxy_secret, token))
        if target is None:
            raise web.HTTPNotFound(text="Unknown media")

        response = await target.api.fetch_external_bytes(
            target.url,
            authorized=target.authorized,
        )
        if not response.get("ok") and target.fallback_url:
            _LOGGER.debug(
                "Domonap media proxy fallback for %s after %s",
                target.url,
                response.get("error"),
            )
            response = await target.api.fetch_external_bytes(
                target.fallback_url,
                authorized=target.fallback_authorized,
            )

        if not response.get("ok"):
            _LOGGER.warning(
                "Domonap media proxy failed for %s: %s",
                target.url,
                response.get("error"),
            )
            raise web.HTTPBadGateway(
                text=str(response.get("error", "Media fetch failed"))
            )

        headers = {
            "Cache-Control": "no-store",
            "Content-Type": response.get("content_type") or "application/octet-stream",
        }
        return web.Response(body=response["body"], headers=headers)

    def _get_base_url(self) -> str | None:
        if get_url is None:
            return None

        for prefer_external in (True, False):
            try:
                return get_url(self._hass, prefer_external=prefer_external)
            except (NoURLAvailableError, TypeError, ValueError):
                continue

        _LOGGER.debug("Unable to build Domonap media proxy URL", exc_info=True)
        return None


class DomonapMediaProxyView(HomeAssistantView):
    url = f"/api/{DOMAIN}/media_proxy/{{proxy_secret}}/{{token}}"
    name = f"api:{DOMAIN}:media_proxy"
    requires_auth = False

    def __init__(self, proxy: DomonapMediaProxy) -> None:
        self._proxy = proxy

    async def get(
        self,
        request: web.Request,
        proxy_secret: str,
        token: str,
    ) -> web.Response:
        return await self._proxy.get_media(proxy_secret, token)
