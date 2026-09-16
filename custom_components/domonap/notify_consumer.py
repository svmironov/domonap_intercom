import json
import logging
import asyncio
import aiohttp
from random import randint
from typing import Callable, Optional, Any, Iterable, Union
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .api import IntercomAPI, _generate_device_token
from .const import (
    EVENT_CALL_ENDED,
    EVENT_INCOMING_CALL,
    WS_MESSAGE_END,
    WS_HANDSHAKE_MESSAGE,
    WS_KEEPALIVE_INTERVAL,
    WS_PING_MESSAGE,
    WS_SERVER_TIMEOUT,
    WS_URL,
)

_LOGGER = logging.getLogger(__name__)


class IntercomNotifyConsumer:
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
        self._notify_id_token: Optional[str] = None
        self._connected: bool = False
        self._username: str = ""
        self._reconnect_delay: int = 1
        self._max_reconnect: int = 10
        self._stop_event = asyncio.Event()
        self._session = async_get_clientsession(hass)
        # Заголовки WebSocket-апгрейда как у SignalR-клиента приложения:
        # User-Agent, dom-app, dom-platform + Bearer (без instanceId/device-info).
        self._headers = dict(self._api.signalr_headers())
        self._headers["Authorization"] = f"Bearer {self._api.access_token or ''}"
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        if hasattr(self._api, "token_update_callback") and self._api.token_update_callback is None:
            self._api.token_update_callback = self._on_token_update

    async def start(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as e:
                if e.status == 401:
                    _LOGGER.error("WS 401 Unauthorized: %s", e.headers.get("WWW-Authenticate"))
                elif e.status == 404:
                    _LOGGER.warning("WS 404 Not found")
                else:
                    _LOGGER.warning("WS handshake error: status=%s %s", e.status, e)
            except Exception as e:
                _LOGGER.warning("Notify loop error: %s: %s", type(e).__name__, e)
            if self._stop_event.is_set():
                break
            delay = self._reconnect_delay
            _LOGGER.info("WS loop ended, reconnecting in %d seconds...", delay)
            await asyncio.sleep(delay)
            self._reconnect_delay = max(1, randint(delay, self._max_reconnect))

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass

    def register_callback(self, callback: Callable[[], Any]) -> None:
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], Any]) -> None:
        self._callbacks.discard(callback)

    @property
    def connected(self) -> bool:
        return self._connected

    def _on_token_update(
        self,
        access: Optional[str],
        _refresh: Optional[str],
        _exp: Optional[str],
    ) -> None:
        self._headers["Authorization"] = f"Bearer {access or ''}"

    async def _connect_and_run(self) -> None:
        # Перед каждым подключением регистрируем свежий device_token: сервер
        # привязывает доставку пушей к последнему зарегистрированному токену,
        # и без перерегистрации уведомления перестают приходить.
        self._api.device_token = _generate_device_token()
        _LOGGER.info("New device_token: %s", self._api.device_token)
        _LOGGER.info("Registering device token before WS connect...")
        try:
            ok = await self._api.update_device_token(self._api.device_token)
            _LOGGER.info("update_device_token result: %s", ok)
        except Exception:
            _LOGGER.warning("update_device_token failed", exc_info=True)

        # The app connects with shouldSkipNegotiate(true): the WebSocket is
        # opened directly on the hub URL without a negotiate round trip, so
        # there is no connectionToken to append.
        ws_url = WS_URL
        self._headers = dict(self._api.signalr_headers())
        self._headers["Authorization"] = f"Bearer {self._api.access_token or ''}"
        self._headers["Sec-WebSocket-Protocol"] = "json"

        await self._ws_connect_and_listen(ws_url)

    async def _ws_connect_and_listen(self, ws_url: str) -> None:
        # receive_timeout = serverTimeout клиента Microsoft SignalR: если за 30с не
        # пришло ни одного сообщения (сервер шлёт свои ping ~каждые 15с), считаем
        # соединение мёртвым — receive() бросит TimeoutError, цикл прервётся и
        # произойдёт переподключение. WS control-ping'и не используем, как и клиент.
        ping_task: Optional[asyncio.Task] = None
        # Отдельная сессия под каждое WS-подключение: у общей сессии HA свой
        # набор заголовков/настроек TLS, а хабу нужны ровно наши заголовки.
        ws_session = aiohttp.ClientSession()
        try:
            async with ws_session.ws_connect(
                ws_url, headers=self._headers, receive_timeout=WS_SERVER_TIMEOUT, ssl=False
            ) as ws:
                self._ws = ws
                _LOGGER.info("WS connected to %s", ws_url)
                self._connected = True
                self._reconnect_delay = 1
                self._username = await self._api.get_username()
                await ws.send_str(WS_HANDSHAKE_MESSAGE)
                ping_task = asyncio.ensure_future(self._keepalive(ws))
                try:
                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_text(msg.data, ws)
                            if self._callbacks:
                                await self._publish_updates()
                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            _LOGGER.debug("WS closed/error: %s", msg.data)
                            break
                finally:
                    if ping_task is not None:
                        ping_task.cancel()
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            # serverTimeout: сервер молчит дольше WS_SERVER_TIMEOUT — штатный
            # признак мёртвого соединения, переподключаемся (не ошибка).
            _LOGGER.warning("WS server timeout, reconnecting")
        finally:
            self._connected = False
            self._username = ""
            self._ws = None
            await ws_session.close()
            _LOGGER.info("WS disconnected")

    async def _keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Периодически шлёт SignalR ping (`{"type":6}`).

        Без этого сервер разрывает соединение по ClientTimeoutInterval, когда
        нет входящих звонков/сообщений, и уведомления перестают приходить.
        """
        try:
            while not ws.closed and not self._stop_event.is_set():
                await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
                if ws.closed or self._stop_event.is_set():
                    break
                try:
                    await ws.send_str(WS_PING_MESSAGE)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_text(self, raw: str, ws: aiohttp.ClientWebSocketResponse) -> None:
        # SignalR может упаковать несколько сообщений в один WebSocket-кадр,
        # разделяя их символом-разделителем записей (0x1e). Разбираем каждую
        # запись отдельно, иначе json.loads падает на составном кадре и все
        # сообщения (включая ReceivePush о звонке) теряются.
        for record in raw.split(WS_MESSAGE_END):
            if not record:
                continue
            await self._handle_record(record, ws)

    async def _handle_record(self, payload: str, ws: aiohttp.ClientWebSocketResponse) -> None:
        if payload == "{}":
            _LOGGER.debug("Handshake ack")
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            _LOGGER.debug("Non-JSON frame: %s", payload[:200])
            return
        t = data.get("type")
        if t == 1:
            await self._handle_invocation(data, ws)
        elif t == 6:
            # Серверный ping. Клиент Microsoft SignalR его НЕ отправляет обратно —
            # он лишь сбрасывает serverTimeout и шлёт собственные ping по таймеру
            # (см. _keepalive). Поэтому просто игнорируем, без эха.
            _LOGGER.debug("Server ping")
        elif t == 3:
            _LOGGER.debug("Completion frame: %s", data)
        else:
            _LOGGER.debug("Unknown frame type=%s data=%s", t, payload[:200])

    async def _handle_invocation(self, data: dict, ws: aiohttp.ClientWebSocketResponse) -> None:
        target = data.get("target")
        args: Iterable = data.get("arguments") or []
        if target == "ReceivePush":
            push_data = args[2] if len(args) >= 3 else None
            if isinstance(push_data, dict):
                evt = push_data.get("EventMessage")
                if evt == "DomofonCalling":
                    self._api.set_active_call(
                        push_data.get("CallId") or push_data.get("callId")
                    )
                    self._api.start_active_sip_call(push_data)
                    await self._prepare_incoming_call_event(push_data)
                    self._hass.bus.fire(EVENT_INCOMING_CALL, push_data)
                    _LOGGER.info("Incoming call fired: DoorId=%s CallId=%s", push_data.get("DoorId"), push_data.get("CallId"))
                elif evt == "DomofonCallEnded":
                    call_id = push_data.get("CallId") or push_data.get("callId")
                    self._api.clear_active_call(
                        call_id
                    )
                    self._hass.bus.fire(EVENT_CALL_ENDED, push_data)
                    # После завершения звонка сервер перестаёт слать пуши в это
                    # соединение — переподключаемся, чтобы поймать следующий звонок.
                    _LOGGER.info("Call ended, forcing WS reconnect for next call")
                    if self._ws is not None and not self._ws.closed:
                        await self._ws.close()
                else:
                    _LOGGER.debug("Unknown EventMessage=%s push=%s", evt, str(push_data)[:200])
        elif target in ('ReceiveOnline', "ReceiveOffline"):
            user = data.get('arguments')[0]
            status = data.get('target').replace('ReceiveO', 'o')

            _LOGGER.debug(f"User {user} is {status}")

            self._hass.bus.fire("domonap_user_status_changed", {
                'user': user,
                'status': status
            })

            # Обработка ситуации когда под одним аккаунтом выполнен вход (реакция на выход) в приложение
            # После события offline на все сессии текущего пользователя перестают приходить уведомления о звонках
            if user == self._username and status == "offline":
                _LOGGER.debug(f"Current login user: {user} status changed to {status}. Reconnecting websocket...")
                # Закрываем текущий сокет: цикл _connect_and_run завершится, и
                # внешний цикл start() автоматически переподключится (в отличие
                # от stop()+start(), которые вызывались рекурсивно из цикла чтения
                # и приводили к вложенным бесконечным циклам).
                if self._ws is not None and not self._ws.closed:
                    try:
                        await self._ws.close()
                    except Exception:
                        _LOGGER.debug("Error closing websocket for reconnect", exc_info=True)

        elif target == "ReceiveMessage":
            chat_data = data.get('arguments')[0]
            self._hass.bus.fire("domonap_receive_message", chat_data)
            _LOGGER.debug(f"Received message from {chat_data.get('sender')}: {chat_data.get('text')}")
        elif target == 'ReceiveRead':
            _LOGGER.debug(f"Read confirm messages in channel {data.get('arguments')[0]}")
        else:
            _LOGGER.debug(f"Unknown target type {data.get('target')} message:\n{data}")

    async def _prepare_incoming_call_event(self, push_data: dict) -> None:
        call_id = str(push_data.get("CallId", ""))
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
                response = await self._api.get_call_logs(per_page=20, current_page=1)
            except Exception:
                _LOGGER.debug("Failed to load Domonap call logs", exc_info=True)
                return None
            if not isinstance(response, dict):
                _LOGGER.debug(
                    "Unexpected Domonap call logs payload: %s",
                    type(response).__name__,
                )
                return None
            if "error" in response:
                _LOGGER.debug("Failed to load Domonap call logs: %s", response)
                return None

            call_logs = response.get("results", [])
            if not isinstance(call_logs, list):
                _LOGGER.debug("Unexpected Domonap call logs results: %s", call_logs)
                return None

            for call_log in call_logs:
                if not isinstance(call_log, dict):
                    continue
                if str(call_log.get("callId", "")) != call_id:
                    continue
                photo_url = call_log.get("photoUrl")
                if photo_url:
                    return photo_url
                return None

        _LOGGER.debug("Call log photoUrl not found for call %s", call_id)
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
            _LOGGER.debug("Failed to register Domonap media proxy URL", exc_info=True)
            return None

    async def _publish_updates(self) -> None:
        for cb in list(self._callbacks):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb()
                else:
                    cb()
            except Exception as e:
                _LOGGER.debug("Callback error: %s", e)
