import json
import logging
import aiohttp
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from secrets import token_bytes
from typing import Any, Callable, Dict, Optional, Union
from uuid import UUID

from .sip import DomonapSipCall
from .const import CALL_END_MODE_ANSWER

_LOGGER = logging.getLogger(__name__)


DEFAULT_DEVICE_PLATFORM = "Android"
DEFAULT_DOM_APP = "mobile"
DEFAULT_JSON_CONTENT_TYPE = "application/json; charset=UTF-8"
DEFAULT_USER_AGENT = "okhttp/5.3.2"
# User-Agent, который клиент Microsoft SignalR (Java, v8.0.6) ставит на negotiate
# и WebSocket-апгрейд. UserAgentHelper.createUserAgentString():
#   "Microsoft SignalR/8.0 (8.0.6; <os.name>; Java; <java.version>; <java.vendor>)"
# На Android: os.name="Linux", java.version="0", java.vendor="The Android Project".
# OkHttp не перезаписывает UA, т.к. SignalR уже задал его в заголовках запроса.
SIGNALR_USER_AGENT = "Microsoft SignalR/8.0 (8.0.6; Linux; Java; 0; The Android Project)"
_ANDROID_GUID_RETRY_LIMIT = 8
_GENERATED_ANDROID_GUIDS: set[str] = set()

# Версия приложения Domonap (BuildConfig.VERSION_CODE / VERSION_NAME из APK).
# Подставляется в заголовок device-info, чтобы совпадать с оригинальным клиентом.
APP_VERSION_CODE = "9848"
APP_VERSION_NAME = "9848"

# Реальные согласованные Build-профили Android-устройств (Google-флейвор,
# DeviceCoreService.Android). Профиль выбирается детерминированно по instanceId,
# поэтому стабилен для установки и различается между установками — как у набора
# реальных телефонов, а не одинаковый на всех.
_DEVICE_PROFILES = (
    # brand, manufacturer, model, device, product, build_id, release, sdk
    ("samsung", "samsung", "SM-S911B", "dm3q", "dm3qxxx", "UP1A.231005.007", "14", "34"),
    ("samsung", "samsung", "SM-A546E", "a54x", "a54xnaser", "UP1A.231005.007", "14", "34"),
    ("samsung", "samsung", "SM-G991B", "o1s", "o1sxxx", "TP1A.220624.014", "13", "33"),
    ("google", "Google", "Pixel 7", "panther", "panther", "UP1A.231105.003", "14", "34"),
    ("google", "Google", "Pixel 6a", "bluejay", "bluejay", "UP1A.231105.001", "14", "34"),
    ("Xiaomi", "Xiaomi", "2211133C", "fuxi", "fuxi", "UKQ1.230804.001", "14", "34"),
    ("Redmi", "Xiaomi", "23021RAA2Y", "ruby", "ruby_global", "TP1A.220624.014", "13", "33"),
    ("OnePlus", "OnePlus", "CPH2449", "OP594DL1", "CPH2449", "UKQ1.230924.001", "14", "34"),
)


def _with_app_header_suffix(value: str) -> str:
    return value if value.endswith(";") else f"{value};"


def _build_device_info(instance_id: str) -> str:
    """Собирает заголовок `device-info` в том же формате, что и приложение.

    Приложение сериализует Gson'ом модель DeviceInfoModel (компактный JSON без
    пробелов) и шлёт его на каждом запросе. Профиль устройства выбирается по
    instanceId, чтобы быть стабильным и правдоподобным.
    """
    idx = int(sha256(instance_id.encode("utf-8")).hexdigest(), 16) % len(_DEVICE_PROFILES)
    brand, manufacturer, model, device, product, build_id, release, sdk = _DEVICE_PROFILES[idx]
    info = {
        "OsVersion": sdk,
        "Release": release,
        "Device": device,
        "Model": model,
        "Product": product,
        "Brand": brand,
        "ID": build_id,
        "Manufacturer": manufacturer,
        "InstanceId": instance_id,
        "versionCode": APP_VERSION_CODE,
        "versionName": APP_VERSION_NAME,
    }
    return json.dumps(info, separators=(",", ":"), ensure_ascii=False)


def _generate_android_guid() -> str:
    random_bytes = bytearray(token_bytes(16))
    # Match Java/Android UUID.randomUUID(): RFC 4122 variant, version 4.
    random_bytes[6] = (random_bytes[6] & 0x0F) | 0x40
    random_bytes[8] = (random_bytes[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(random_bytes)))


def _generate_unique_android_guid() -> str:
    for _ in range(_ANDROID_GUID_RETRY_LIMIT):
        guid = _generate_android_guid()
        if guid not in _GENERATED_ANDROID_GUIDS:
            _GENERATED_ANDROID_GUIDS.add(guid)
            return guid
    guid = _generate_android_guid()
    _GENERATED_ANDROID_GUIDS.add(guid)
    return guid


def _generate_device_token() -> str:
    return _generate_unique_android_guid()


def is_android_guid(value: Optional[str]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        guid = UUID(value)
    except ValueError:
        return False
    return guid.version == 4 and value == str(guid)


class IntercomAPI:
    def __init__(
        self,
        base_url: str = "https://api.domonap.ru",
        device_token: Optional[str] = None,
        instance_id: Optional[str] = None,
        device_platform: str = DEFAULT_DEVICE_PLATFORM,
        dom_app: str = DEFAULT_DOM_APP,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.refresh_expiration_date: Optional[str] = None
        self.device_token = device_token or _generate_device_token()
        self.instance_id = instance_id or _generate_unique_android_guid()
        self.device_platform = device_platform
        self.dom_app = dom_app
        self._refresh_token_invalid: bool = False
        self._refresh_lock = asyncio.Lock()
        self._active_call_id: Optional[str] = None
        self._active_call_lock = asyncio.Lock()
        self._active_sip_call: Optional[DomonapSipCall] = None
        self._active_sip_call_id: Optional[str] = None
        # How an active call is ended around relay actions: "answer" accepts it
        # first (200 OK mutes the panel) and hangs up with BYE, "reject" declines
        # with 603 right away.
        self.call_end_mode: str = CALL_END_MODE_ANSWER
        # Порядок и формат заголовков как у DeviceIdInterceptor приложения:
        # dom-app/dom-platform с суффиксом ";", instanceId — БЕЗ ";", плюс
        # device-info с JSON профиля устройства.
        self.headers: Dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "dom-app": _with_app_header_suffix(self.dom_app),
            "dom-platform": _with_app_header_suffix(self.device_platform),
            "instanceId": self.instance_id,
            "device-info": _build_device_info(self.instance_id),
        }
        self.token_update_callback: Optional[
            Callable[[Optional[str], Optional[str], Optional[str]], None]
        ] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._external_session: Optional[aiohttp.ClientSession] = None
        self._closed = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if not self._session or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self._session

    async def _ensure_external_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if not self._external_session or self._external_session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._external_session = aiohttp.ClientSession(timeout=timeout)
        return self._external_session

    async def close(self):
        self._closed = True
        if self._active_sip_call is not None:
            await self._active_sip_call.stop()
            self._active_sip_call = None
            self._active_sip_call_id = None
        if self._session and not self._session.closed:
            await self._session.close()
        if self._external_session and not self._external_session.closed:
            await self._external_session.close()

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def set_tokens(
        self,
        access_token: Optional[str],
        refresh_token: Optional[str],
        refresh_expiration_date: Optional[str],
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.refresh_expiration_date = refresh_expiration_date
        if refresh_token:
            self._refresh_token_invalid = False
        self.headers.pop("Authorization", None)
        if self._session and not self._session.closed:
            self._session._default_headers.clear()
            self._session._default_headers.update(self.headers)

    def signalr_headers(self) -> Dict[str, str]:
        """Заголовки для SignalR (WebSocket-апгрейд хаба).

        В приложении hub использует отдельный OkHttp-клиент, который в DI-колбэке
        (`provideSignalR`) получает только DeviceCoreServicesRepository, поэтому
        добавляет `dom-app`/`dom-platform`, но НЕ instanceId и НЕ device-info
        (у него нет соответствующих репозиториев). Авторизацию (Bearer) добавляет
        вызывающий код. User-Agent — как у клиента Microsoft SignalR, а не okhttp.
        """
        return {
            "User-Agent": SIGNALR_USER_AGENT,
            "dom-app": self.headers["dom-app"],
            "dom-platform": self.headers["dom-platform"],
        }

    def _parse_dt(self, val: str) -> Optional[datetime]:
        fmts = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
        for fmt in fmts:
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            pass
        _LOGGER.warning("Cannot parse datetime: %s", val)
        return None

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _refresh_expired(self) -> bool:
        if not self.refresh_token or not self.refresh_expiration_date:
            return False
        exp = self._parse_dt(self.refresh_expiration_date)
        return bool(exp and self._now_utc() >= exp)

    def has_valid_refresh_token(self) -> bool:
        return bool(
            self.refresh_token
            and not self._refresh_token_invalid
            and not self._refresh_expired()
        )

    def mark_session_expired(self, reason: str) -> None:
        self._mark_refresh_token_invalid(reason)

    def _mark_refresh_token_invalid(self, reason: str) -> None:
        if self._refresh_token_invalid and not self.refresh_token and not self.access_token:
            return
        _LOGGER.warning("Domonap session expired: %s", reason)
        self._refresh_token_invalid = True
        self.access_token = None
        self.refresh_token = None
        self.refresh_expiration_date = None
        self.headers.pop("Authorization", None)
        if self.token_update_callback:
            self.token_update_callback(None, None, None)

    def _refresh_unavailable_error(self, error: str) -> Dict[str, Any]:
        return {
            "error": error,
            "ok": False,
            "session_expired": self._refresh_token_invalid,
            "body": "",
        }

    def _ensure_refresh_is_available(self) -> bool:
        if self._refresh_token_invalid:
            return False
        if self._refresh_expired():
            self._mark_refresh_token_invalid("refresh token expired")
            return False
        return bool(self.refresh_token)

    async def _ensure_alive(self) -> None:
        if self._refresh_expired():
            self._mark_refresh_token_invalid("refresh token expired")

    async def _refresh_for_retry(self, first_try_access_token: Optional[str]) -> bool:
        if not self._ensure_refresh_is_available():
            return False
        async with self._refresh_lock:
            if (
                first_try_access_token
                and self.access_token
                and self.access_token != first_try_access_token
            ):
                return True
            if not self._ensure_refresh_is_available():
                return False
            result = await self.update_token()
            return bool(isinstance(result, dict) and result.get("ok"))

    async def _post(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        need_auth: bool = False,
        ensure_alive: bool = True,
        send_auth: Optional[bool] = None,
        expect: str = "json",
        retry_on_401: bool = True,
        header_set: Optional[Dict[str, str]] = None,
    ) -> Union[Dict[str, Any], str]:
        if send_auth is None:
            send_auth = need_auth
        if need_auth:
            if self._refresh_token_invalid:
                return self._refresh_unavailable_error("Session expired")
            if not self.access_token:
                return {"error": "No access token available", "ok": False, "body": ""}
            if ensure_alive:
                await self._ensure_alive()
            if not self.access_token:
                return self._refresh_unavailable_error("Session expired")

        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        first_try_access_token = self.access_token

        async def _do() -> aiohttp.ClientResponse:
            headers = dict(self.headers if header_set is None else header_set)
            if payload is not None:
                headers["Content-Type"] = DEFAULT_JSON_CONTENT_TYPE
            if send_auth and self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            if payload is None:
                return await session.post(url, headers=headers, ssl=False)
            return await session.post(url, json=payload, headers=headers, ssl=False)

        resp = await _do()
        if resp.status == 401 and retry_on_401 and self.refresh_token:
            _LOGGER.warning("401 Unauthorized, refreshing token and retrying %s", path)
            if await self._refresh_for_retry(first_try_access_token):
                resp = await _do()

        if 200 <= resp.status < 300:
            if expect == "json":
                return await resp.json()
            return await resp.text()

        body_text = ""
        try:
            body_text = await resp.text()
        except Exception:
            pass
        err = {"error": f"HTTP {resp.status}", "status": resp.status, "body": body_text[:2000]}
        _LOGGER.error("Request failed: POST %s payload=%s -> %s", path, payload, err)
        return err

    async def update_device_token(self, device_token: str) -> bool:
        _LOGGER.debug("UpdateDeviceToken start")
        result = await self._post(
            "/sso-api/Authorization/UpdateDeviceToken",
            {"deviceToken": device_token, "platform": self.device_platform},
            need_auth=True,
            ensure_alive=False,
            expect="text",
            retry_on_401=True,
        )
        if isinstance(result, dict) and "error" in result:
            _LOGGER.error("UpdateDeviceToken failed: %s", result)
            return False
        _LOGGER.debug("UpdateDeviceToken ok")
        return True

    async def authorize(self, country_code: str, phone_number: str) -> Union[bool, Dict[str, Any]]:
        payload = {"phoneNumber": self._phone_number(country_code, phone_number)}
        res = await self._post("/sso-api/Authorization/Authorize", payload, expect="text", need_auth=False)
        if isinstance(res, dict) and "error" in res:
            return {"error": f"Authorization failed: {res}"}
        return True

    async def confirm_authorization(
        self,
        country_code: str,
        phone_number: str,
        confirm_code: str,
        device_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "phoneNumber": self._phone_number(country_code, phone_number),
            "confirmCode": confirm_code,
            "deviceToken": device_token or self.device_token,
        }
        res = await self._post("/sso-api/Authorization/ConfirmAuthorization", payload, expect="json", need_auth=False)
        if isinstance(res, dict) and "error" in res and "status" in res:
            return res
        try:
            ct = res["completeToken"]
            self.set_tokens(ct["accessToken"], ct["refreshToken"], ct["refreshExpirationDate"])
            if self.token_update_callback:
                self.token_update_callback(ct["accessToken"], ct["refreshToken"], ct["refreshExpirationDate"])
            await self.update_device_token(device_token or self.device_token)
        except Exception as e:
            _LOGGER.exception("Unexpected response on confirm_authorization: %s", e)
        return res

    def _phone_number(self, country_code: str, phone_number: str) -> Dict[str, int]:
        return {"countryCode": int(country_code), "number": int(phone_number)}

    async def update_token(self) -> Dict[str, Any]:
        if self._refresh_token_invalid:
            return self._refresh_unavailable_error("Refresh token is invalid")
        if not self.refresh_token:
            return {"error": "No refresh token available", "ok": False, "body": ""}
        if self._refresh_expired():
            self._mark_refresh_token_invalid("refresh token expired")
            return self._refresh_unavailable_error("Refresh token expired")
        _LOGGER.info("Begin refreshToken. Old refresh_expiration=%s now=%s", self.refresh_expiration_date, self._now_utc())
        res = await self._post(
            "/sso-api/Authorization/RefreshToken",
            {"refreshToken": self.refresh_token},
            expect="json",
            need_auth=False,
            retry_on_401=False,
        )
        if isinstance(res, dict) and "error" in res and "status" in res:
            if res["status"] in (400, 401, 403):
                self._mark_refresh_token_invalid(f"refresh token rejected with HTTP {res['status']}")
            return res
        try:
            self.set_tokens(res["accessToken"], res["refreshToken"], res["refreshExpirationDate"])
            _LOGGER.info("Tokens refreshed. New refresh_expiration=%s", res["refreshExpirationDate"])
            if self.token_update_callback:
                self.token_update_callback(res["accessToken"], res["refreshToken"], res["refreshExpirationDate"])
            return {
                "ok": True,
                "access_token": res["accessToken"],
                "refresh_token": res["refreshToken"],
                "refresh_expiration_date": res["refreshExpirationDate"],
            }
        except Exception as e:
            _LOGGER.exception("Unexpected refresh response: %s", e)
            return {"error": "Unexpected refresh response format", "ok": False, "body": str(res)}

    async def get_user(self) -> Union[Dict[str, Any], str]:
        return await self._post("/sso-api/User/GetUser", need_auth=True, expect="json")

    async def get_username(self):
        user = await self.get_user()
        if user:
            return user.get("userProfile").get("username")

    async def get_paged_keys(self, per_page: int = 100, current_page: int = 1, keys_type: str = "Main"):
        payload = {
            "currentPage": current_page,
            "perPage": per_page,
            "keysType": keys_type,
            "search": None,
        }
        return await self._post("/client-api/Key/GetPagedKeysByKeysType", payload, need_auth=True, expect="json")

    async def get_video_area(self):
        return await self._post(
            "/client-api/VideoCamera/GetVideoArea",
            need_auth=True,
            expect="json",
        )

    async def get_user_video_cameras(self, category: str):
        payload = {"category": category}
        return await self._post(
            "/client-api/VideoCamera/GetUserVideoCameras",
            payload,
            need_auth=True,
            expect="json",
        )

    async def get_user_key(self, key_id: str):
        payload = {"keyId": key_id}
        return await self._post("/client-api/Key/GetUserKey", payload, need_auth=True, expect="json")

    async def get_call_logs(
        self,
        per_page: int = 20,
        current_page: int = 1,
        missed_calls: bool = False,
    ):
        payload = {
            "currentPage": current_page,
            "perPage": per_page,
            "missedCalls": missed_calls,
        }
        return await self._post(
            "/client-api/CallLog/GetCallLogs",
            payload,
            need_auth=True,
            expect="json",
        )

    async def open_relay_by_door_id(self, door_id: str):
        payload = {"doorId": door_id}
        await self._answer_active_sip_before_open()
        res = await self._post("/client-api/Device/OpenRelayByDoorId", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        return {"ok": True, "body": res}

    async def open_relay_by_key_id(self, key_id: str):
        payload = {"keyId": key_id}
        await self._answer_active_sip_before_open()
        res = await self._post("/client-api/Device/OpenRelayByKeyId", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        return {"ok": True, "body": res}

    async def _answer_active_sip_before_open(self, *, force: bool = False) -> Dict[str, Any] | None:
        """Answer the active SIP call before a relay action, like the app.

        The app accepts the incoming call before requesting the relay opening.
        That ordering matters for forked calls: rejecting our still-ringing
        branch does not stop the originating panel while accepting it does.
        Answering sends a 200 OK with a no-media SDP, so the panel goes silent
        without any audio flowing through Home Assistant.

        Skipped in the "reject" call-end mode unless ``force`` is set (the
        silence service always mutes the panel regardless of the mode).
        """
        if not force and self.call_end_mode != CALL_END_MODE_ANSWER:
            return None
        sip_call = self._active_sip_call
        answer = getattr(sip_call, "answer", None)
        if not self._active_call_id or not callable(answer):
            return None
        try:
            result = await sip_call.answer(timeout=2.0)
        except Exception as err:
            _LOGGER.warning("SIP answer before relay opening failed: %s", err)
            return {"ok": False, "error": str(err)}
        if not (isinstance(result, dict) and result.get("ok") is True):
            _LOGGER.warning("SIP answer before relay opening failed: %s", result)
        else:
            _LOGGER.info(
                "SIP call %s answered before relay opening",
                self._active_call_id,
            )
        return result

    @property
    def active_call_id(self) -> Optional[str]:
        """Return the call currently reported as active by the notification hub."""
        return self._active_call_id

    def set_active_call(self, call_id: Optional[str]) -> None:
        """Update the active call reported by the notification hub."""
        normalized_call_id = str(call_id).strip() if call_id is not None else ""
        self._active_call_id = normalized_call_id or None

    def start_active_sip_call(self, push_data: dict[str, Any]) -> None:
        """Start the SIP registration used by the APK for an incoming call."""
        raw_call_id = push_data.get("CallId") or push_data.get("callId")
        call_id = str(raw_call_id).strip() if raw_call_id is not None else ""
        if (
            call_id
            and call_id == self._active_sip_call_id
            and self._active_sip_call is not None
        ):
            return
        sip_data = push_data.get("SipData") or push_data.get("sipData") or push_data
        if not isinstance(sip_data, dict):
            return
        account = (
            sip_data.get("SipAccount")
            or sip_data.get("sipAccount")
            or sip_data.get("account")
        )
        password = (
            sip_data.get("SipPassword")
            or sip_data.get("sipPassword")
            or sip_data.get("password")
        )
        domain = (
            sip_data.get("SipDomain")
            or sip_data.get("sipDomain")
            or sip_data.get("domain")
        )
        raw_port = (
            sip_data.get("SipPort")
            or sip_data.get("sipPort")
            or sip_data.get("port")
        )
        if not all((account, password, domain, raw_port)):
            _LOGGER.debug("Incoming call does not contain complete SIP credentials")
            return
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid Domonap SIP port: %s", raw_port)
            return

        previous = self._active_sip_call
        if previous is not None:
            asyncio.create_task(previous.stop())
        self._active_sip_call = DomonapSipCall(
            str(account), str(password), str(domain), port
        )
        self._active_sip_call_id = call_id or self._active_call_id
        self._active_sip_call.start()

    def clear_active_call(self, call_id: Optional[str] = None) -> None:
        """Clear the active call, optionally only when its id still matches."""
        normalized_call_id = str(call_id).strip() if call_id is not None else ""
        if not normalized_call_id or self._active_call_id == normalized_call_id:
            self._active_call_id = None
            sip_call = self._active_sip_call
            self._active_sip_call = None
            self._active_sip_call_id = None
            if sip_call is not None:
                asyncio.create_task(sip_call.stop())

    async def end_active_call(self):
        """End the current call once and return the API response, if any."""
        async with self._active_call_lock:
            call_id = self._active_call_id
            if not call_id:
                return None

            sip_call = self._active_sip_call
            if sip_call is None:
                sip_result = None
                notify_result = await self.end_call_notify(call_id)
            else:
                try:
                    sip_result = await sip_call.end()
                except Exception as err:
                    sip_result = {"ok": False, "error": str(err)}
                if isinstance(sip_result, dict) and sip_result.get("ok") is True:
                    notify_result = None
                else:
                    _LOGGER.warning(
                        "SIP call termination unavailable for %s (%s); using REST fallback",
                        call_id,
                        sip_result,
                    )
                    notify_result = await self.end_call_notify(call_id)
            sip_ok = isinstance(sip_result, dict) and sip_result.get("ok") is True
            notify_ok = (
                isinstance(notify_result, dict)
                and notify_result.get("ok") is True
            )
            result = {
                "ok": sip_ok or notify_ok,
                "sip": sip_result,
                "notify": notify_result,
            }
            if result["ok"]:
                self.clear_active_call(call_id)
            return result

    async def fetch_external_bytes(
        self,
        url: str,
        *,
        authorized: bool = True,
        headers: Optional[Dict[str, str]] = None,
        retry_on_401: bool = True,
    ) -> Dict[str, Any]:
        if authorized:
            auth_error = await self._ensure_external_auth()
            if auth_error:
                return auth_error

        session = await self._ensure_external_session()
        first_try_access_token = self.access_token

        def _request():
            request_headers = dict(headers or {})
            if authorized:
                request_headers = self._authorized_external_headers(request_headers)
            return session.get(url, headers=request_headers)

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            body = await resp.read()
            if 200 <= resp.status < 300:
                return {
                    "ok": True,
                    "status": resp.status,
                    "body": body,
                    "content_type": resp.headers.get("Content-Type"),
                }
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": body[:2000].decode("utf-8", "replace"),
            }

        try:
            async with _request() as resp:
                if (
                    resp.status == 401
                    and authorized
                    and retry_on_401
                    and self.refresh_token
                ):
                    _LOGGER.warning(
                        "401 Unauthorized, refreshing token and retrying external GET %s",
                        url,
                    )
                    if await self._refresh_for_retry(first_try_access_token):
                        async with _request() as retry_resp:
                            return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("External GET failed: %s -> %s", url, err)
            return {"ok": False, "error": str(err), "body": ""}

    def _authorized_external_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        request_headers = dict(headers)
        if self.access_token:
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        return request_headers

    async def _ensure_external_auth(self) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            return {"ok": False, "error": "No access token available", "body": ""}
        await self._ensure_alive()
        if not self.access_token:
            return self._refresh_unavailable_error("Session expired")
        return None

    async def create_whep_session(self, whep_url: str, offer_sdp: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()
        first_try_access_token = self.access_token

        def _request():
            return session.post(
                whep_url,
                data=offer_sdp,
                headers=self._authorized_external_headers(
                    {
                        "Content-Type": "application/sdp",
                        "Accept": "application/sdp",
                    }
                ),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            answer_sdp = await resp.text()
            if resp.status != 201:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status}",
                    "status": resp.status,
                    "body": answer_sdp[:2000],
                }

            location = resp.headers.get("Location")
            if not location:
                return {
                    "ok": False,
                    "error": "WHEP response did not include a session URL",
                    "status": resp.status,
                    "body": answer_sdp[:2000],
                }

            return {
                "ok": True,
                "status": resp.status,
                "answer_sdp": answer_sdp,
                "location": location,
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP offer %s", whep_url)
                    if await self._refresh_for_retry(first_try_access_token):
                        async with _request() as retry_resp:
                            return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("WHEP offer failed: %s -> %s", whep_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def send_whep_candidates(self, session_url: str, sdp_fragment: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()
        first_try_access_token = self.access_token

        def _request():
            return session.patch(
                session_url,
                data=sdp_fragment,
                headers=self._authorized_external_headers(
                    {
                        "Content-Type": "application/trickle-ice-sdpfrag",
                        "If-Match": "*",
                    }
                ),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            if resp.status in (200, 204):
                return {"ok": True, "status": resp.status}

            body = await resp.text()
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": body[:2000],
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP candidate %s", session_url)
                    if await self._refresh_for_retry(first_try_access_token):
                        async with _request() as retry_resp:
                            return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("WHEP candidate failed: %s -> %s", session_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def close_whep_session(self, session_url: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()
        first_try_access_token = self.access_token

        def _request():
            return session.delete(
                session_url,
                headers=self._authorized_external_headers({}),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            if resp.status in (200, 204):
                return {"ok": True, "status": resp.status}
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": await resp.text(),
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP close %s", session_url)
                    if await self._refresh_for_retry(first_try_access_token):
                        async with _request() as retry_resp:
                            return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("WHEP session close failed: %s -> %s", session_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def end_call_notify(self, call_id: str):
        payload = {"callId": call_id}
        res = await self._post("/communication-api/Call/NotifyCallEnded", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        _LOGGER.debug("end_call_notify(%s) -> %s", call_id, res)
        return {"ok": True, "body": res}
