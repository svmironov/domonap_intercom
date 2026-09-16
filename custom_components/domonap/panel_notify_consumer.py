from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Iterable, Optional, Union

import aiohttp
from homeassistant.core import HomeAssistant

from .api import IntercomAPI
from .const import (
    EVENT_CALL_ANSWERED,
    EVENT_CALL_ENDED,
    EVENT_INCOMING_CALL,
    PANEL_WS_HANDSHAKE_TIMEOUT,
    PANEL_WS_KEEPALIVE_INTERVAL,
    PANEL_WS_RECONNECT_INITIAL,
    PANEL_WS_RECONNECT_MAX,
    PANEL_WS_SERVER_TIMEOUT,
    PANEL_WS_URL,
    WS_HANDSHAKE_MESSAGE,
    WS_MESSAGE_END,
    WS_PING_MESSAGE,
)

_LOGGER = logging.getLogger(__name__)


class RubetekPanelNotifyConsumer:
    """SignalR transport used only by the Rubetek/AOSP panel profile.

    Unlike the legacy phone/SMS consumer this transport does not touch a mobile
    deviceToken and does not negotiate a connectionToken. prodAospRelease opens
    /notificationHub directly and authenticates the WebSocket with the Panel
    bearer token.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: IntercomAPI,
        media_proxy=None,
        media_proxy_secret: Optional[str] = None,
    ) -> None:
        self._hass = hass
        self._api = api
        self._media_proxy = media_proxy
        self._media_proxy_secret = media_proxy_secret
        self._callbacks: set[Callable[[], Union[None, Any]]] = set()
        self._connected = False
        self._stop_event = asyncio.Event()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    @property
    def connected(self) -> bool:
        return self._connected

    def register_callback(self, callback: Callable[[], Any]) -> None:
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], Any]) -> None:
        self._callbacks.discard(callback)

    async def start(self) -> None:
        """Keep the direct panel SignalR connection alive."""
        self._stop_event.clear()
        delay = PANEL_WS_RECONNECT_INITIAL
        first_start = True

        while not self._stop_event.is_set():
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

            connected = False
            auth_refreshed = False
            try:
                connected = await self._connect_and_run()
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                if err.status == 401 and self._api.refresh_token:
                    _LOGGER.info("Panel SignalR token rejected; refreshing session")
                    try:
                        result = await self._api.update_token()
                        auth_refreshed = bool(
                            isinstance(result, dict) and result.get("ok")
                        )
                    except Exception:
                        _LOGGER.warning("Panel token refresh failed", exc_info=True)
                else:
                    _LOGGER.warning(
                        "Panel SignalR handshake failed: status=%s %s",
                        err.status,
                        err,
                    )
            except Exception as err:
                _LOGGER.warning(
                    "Panel SignalR error: %s: %s", type(err).__name__, err
                )

            if self._stop_event.is_set():
                break

            if auth_refreshed or connected:
                # Retry immediately after a successful refresh or after an
                # established connection closes unexpectedly.
                delay = 0
            elif first_start:
                delay = min(PANEL_WS_RECONNECT_INITIAL * 2, PANEL_WS_RECONNECT_MAX)
            elif delay <= 0:
                delay = PANEL_WS_RECONNECT_INITIAL
            else:
                delay = min(delay * 2, PANEL_WS_RECONNECT_MAX)
            first_start = False

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close(code=1000, message=b"HubConnection stopped.")
            except Exception:
                _LOGGER.debug("Error stopping panel SignalR", exc_info=True)

    async def _connect_and_run(self) -> bool:
        headers = dict(self._api.signalr_headers())
        headers["Authorization"] = f"Bearer {self._api.access_token or ''}"

        timeout = aiohttp.ClientTimeout(total=None)
        handshake_completed = False
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(
                    PANEL_WS_URL,
                    headers=headers,
                    receive_timeout=PANEL_WS_SERVER_TIMEOUT,
                    autoping=False,
                ) as ws:
                    self._ws = ws
                    _LOGGER.info("Panel SignalR connected to %s", PANEL_WS_URL)

                    # Microsoft SignalR Java sends HubProtocol ByteBuffers, which
                    # OkHttp writes as binary WebSocket frames.
                    await ws.send_bytes(WS_HANDSHAKE_MESSAGE.encode("utf-8"))
                    await self._wait_for_handshake(ws)
                    handshake_completed = True
                    self._connected = True
                    _LOGGER.info("Panel SignalR handshake completed")

                    ping_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for msg in ws:
                            if self._stop_event.is_set():
                                break
                            await self._handle_ws_message(msg, ws)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as err:
            if not handshake_completed:
                raise
            _LOGGER.warning("Panel SignalR server timeout: %s", err)
        finally:
            self._connected = False
            self._ws = None
            if handshake_completed:
                _LOGGER.info("Panel SignalR disconnected")
        return handshake_completed

    async def _wait_for_handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PANEL_WS_HANDSHAKE_TIMEOUT

        while not self._stop_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Panel SignalR handshake timeout")

            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                raise RuntimeError("Panel WebSocket closed before SignalR handshake")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError(f"Panel WebSocket error: {ws.exception()}")

            payload = self._payload(msg)
            if payload is None:
                continue
            for record in payload.split(WS_MESSAGE_END):
                if record == "{}":
                    return
                if not record:
                    continue
                try:
                    data = json.loads(record)
                except json.JSONDecodeError:
                    continue
                if data.get("error") and data.get("type") is None:
                    raise RuntimeError(
                        f"Panel SignalR handshake failed: {data.get('error')}"
                    )

        raise RuntimeError("Panel SignalR stopped before handshake")

    async def _keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while not ws.closed and not self._stop_event.is_set():
                await asyncio.sleep(PANEL_WS_KEEPALIVE_INTERVAL)
                if ws.closed or self._stop_event.is_set():
                    break
                await ws.send_bytes(WS_PING_MESSAGE.encode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Panel SignalR keepalive stopped", exc_info=True)

    async def _handle_ws_message(
        self, msg: aiohttp.WSMessage, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        if msg.type == aiohttp.WSMsgType.PING:
            await ws.pong(msg.data)
            return
        if msg.type in (aiohttp.WSMsgType.PONG, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            return
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise RuntimeError(f"Panel WebSocket error: {ws.exception()}")

        payload = self._payload(msg)
        if payload is None:
            return
        for record in payload.split(WS_MESSAGE_END):
            if record:
                await self._handle_record(record)
        if self._callbacks:
            await self._publish_updates()

    @staticmethod
    def _payload(msg: aiohttp.WSMessage) -> Optional[str]:
        if msg.type == aiohttp.WSMsgType.TEXT:
            return msg.data
        if msg.type == aiohttp.WSMsgType.BINARY:
            try:
                return msg.data.decode("utf-8")
            except UnicodeDecodeError:
                _LOGGER.debug("Non-UTF8 panel SignalR frame (%d bytes)", len(msg.data))
        return None

    async def _handle_record(self, payload: str) -> None:
        if payload == "{}":
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            _LOGGER.debug("Non-JSON panel SignalR frame: %s", payload[:300])
            return

        message_type = data.get("type")
        if message_type == 1:
            await self._handle_invocation(data)
        elif message_type == 6:
            _LOGGER.debug("Panel SignalR server ping")
        elif message_type == 7:
            raise RuntimeError(
                f"Panel SignalR closed: {data.get('error') or data}"
            )
        else:
            _LOGGER.debug(
                "Panel SignalR frame type=%s data=%s",
                message_type,
                payload[:300],
            )

    async def _handle_invocation(self, data: dict[str, Any]) -> None:
        target = data.get("target")
        args: Iterable[Any] = data.get("arguments") or []
        args = list(args)
        _LOGGER.debug("Panel SignalR invocation: target=%s args=%d", target, len(args))

        if target == "ReceivePush":
            title = args[0] if len(args) >= 1 else ""
            body = args[1] if len(args) >= 2 else ""
            push_data = args[2] if len(args) >= 3 else None
            if not isinstance(push_data, dict):
                return

            push_data = dict(push_data)
            push_data.setdefault("Title", title or "")
            push_data.setdefault("Body", body or "")
            event_message = push_data.get("EventMessage")

            if event_message == "DomofonCalling":
                await self._prepare_incoming_call_event(push_data)
                self._hass.bus.fire(EVENT_INCOMING_CALL, push_data)
                _LOGGER.info(
                    "Panel incoming call: DoorId=%s CallId=%s",
                    push_data.get("DoorId"),
                    push_data.get("CallId"),
                )
            elif event_message == "DomofonCallAnswered":
                self._hass.bus.fire(EVENT_CALL_ANSWERED, push_data)
                _LOGGER.info(
                    "Panel call answered: CallId=%s", push_data.get("CallId")
                )
            elif event_message == "DomofonCallEnded":
                # prodAospRelease treats call end as a domain event; it does not
                # rebuild the SignalR connection just because a call finished.
                self._hass.bus.fire(EVENT_CALL_ENDED, push_data)
                _LOGGER.info("Panel call ended: CallId=%s", push_data.get("CallId"))
            else:
                _LOGGER.debug(
                    "Panel ReceivePush EventMessage=%s payload=%s",
                    event_message,
                    str(push_data)[:500],
                )
            return

        if target in ("ReceiveOnline", "ReceiveOffline"):
            user = args[0] if args else None
            status = "online" if target == "ReceiveOnline" else "offline"
            self._hass.bus.fire(
                "domonap_user_status_changed",
                {"user": user, "status": status, "arguments": args},
            )
            return

        if target == "ReceiveMessage":
            chat_data = args[0] if args else {}
            self._hass.bus.fire("domonap_receive_message", chat_data)
            return

        if target == "ReceiveRead":
            self._hass.bus.fire("domonap_receive_read", {"arguments": args})
            return

        if target == "ReceiveTyping":
            self._hass.bus.fire("domonap_receive_typing", {"arguments": args})
            return

    async def _prepare_incoming_call_event(self, push_data: dict[str, Any]) -> None:
        """Normalize media fields to the same event contract as phone mode."""
        call_id = str(push_data.get("CallId", ""))

        # The APK DomofonPush carries WebrtcVideoUrl and uses it as the live
        # WHEP preview of the incoming call. Keep the original value and add
        # the camelCase alias used by key/camera payloads.
        webrtc_video_url = (
            push_data.get("WebrtcVideoUrl") or push_data.get("webrtcVideoUrl")
        )
        if webrtc_video_url:
            push_data.setdefault("OriginalWebrtcVideoUrl", webrtc_video_url)
            push_data["WebrtcVideoUrl"] = webrtc_video_url
            push_data["webrtcVideoUrl"] = webrtc_video_url

        video_preview = push_data.get("VideoPreview") or push_data.get("videoPreview")
        proxied_video_preview = self._proxied_media_url(video_preview)
        if video_preview:
            push_data.setdefault("OriginalVideoPreview", video_preview)
            push_data["VideoPreview"] = proxied_video_preview or video_preview
            push_data["videoPreview"] = proxied_video_preview or video_preview

        push_photo_url = push_data.get("PhotoUrl") or push_data.get("photoUrl")
        if push_photo_url:
            push_data.setdefault("PushPhotoUrl", push_photo_url)

        photo_url = await self._get_call_log_photo_url(call_id)
        proxied_photo_url = self._proxied_media_url(
            photo_url,
            fallback_url=video_preview,
            authorized=False,
        )
        if photo_url:
            push_data.setdefault("OriginalPhotoUrl", photo_url)
            push_data["PhotoUrl"] = proxied_photo_url or photo_url
            push_data["photoUrl"] = proxied_photo_url or photo_url
        elif video_preview:
            push_data["PhotoUrl"] = proxied_video_preview or video_preview
            push_data["photoUrl"] = proxied_video_preview or video_preview

    async def _get_call_log_photo_url(self, call_id: str) -> Optional[str]:
        if not call_id:
            return None

        for attempt in range(3):
            if attempt:
                await asyncio.sleep(1)
            try:
                response = await self._api.get_call_logs(
                    per_page=20,
                    current_page=1,
                )
            except Exception:
                _LOGGER.debug("Failed to load panel call logs", exc_info=True)
                return None
            if not isinstance(response, dict) or "error" in response:
                return None

            call_logs = response.get("results", [])
            if not isinstance(call_logs, list):
                return None
            for call_log in call_logs:
                if not isinstance(call_log, dict):
                    continue
                if str(call_log.get("callId", "")) != call_id:
                    continue
                return call_log.get("photoUrl")
        return None

    def _proxied_media_url(
        self,
        url: Optional[str],
        *,
        fallback_url: Optional[str] = None,
        authorized: bool = True,
        fallback_authorized: bool = True,
    ) -> Optional[str]:
        if not url or not self._media_proxy or not self._media_proxy_secret:
            return None
        try:
            return self._media_proxy.register_url(
                self._media_proxy_secret,
                self._api,
                url,
                fallback_url=fallback_url,
                authorized=authorized,
                fallback_authorized=fallback_authorized,
            )
        except Exception:
            _LOGGER.debug("Failed to register panel media proxy URL", exc_info=True)
            return None

    async def _publish_updates(self) -> None:
        for callback in tuple(self._callbacks):
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _LOGGER.exception("Panel notify callback failed")
