from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from secrets import token_hex
from typing import Any, Dict, Optional

from .api import IntercomAPI, SIGNALR_USER_AGENT
from .panel_sip import RubetekPanelSipCall

_LOGGER = logging.getLogger(__name__)

PANEL_APP_VERSION_CODE = "9845"
PANEL_APP_VERSION_NAME = "9845"
_PANEL_INSTANCE_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_PANEL_ROLE_CLAIM = (
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
)
_PANEL_NAME_CLAIM = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"
)


def _build_panel_device_info(instance_id: str) -> str:
    """Build the prodAospRelease device-info observed during panel activation.

    The values intentionally describe the AOSP emulator profile used to activate
    the captured Panel session. The important contract is the PascalCase JSON
    shape, stable 16-hex InstanceId and APK version 9845.
    """
    info = {
        "Brand": "Android",
        "Device": "emulator64_x86_64",
        "ID": "SE1B.240122.005",
        "InstanceId": instance_id,
        "Manufacturer": "unknown",
        "Model": "Android SDK built for x86_64",
        "OsVersion": "5.10.101-android12-9-00027-g1292f517889e-ab8602202",
        "Product": "sdk_phone64_x86_64",
        "Release": "12",
        "versionCode": PANEL_APP_VERSION_CODE,
        "versionName": PANEL_APP_VERSION_NAME,
    }
    return json.dumps(info, separators=(",", ":"), ensure_ascii=False)


def _normalize_panel_device_info(value: Any, instance_id: str) -> str:
    if value is None:
        return _build_panel_device_info(instance_id)
    if isinstance(value, str):
        parsed = json.loads(value)
    elif isinstance(value, dict):
        parsed = dict(value)
    else:
        raise ValueError("panel deviceInfo must be a JSON object")
    if not isinstance(parsed, dict):
        raise ValueError("panel deviceInfo must be a JSON object")
    parsed["InstanceId"] = instance_id
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as err:
        raise ValueError("accessToken is not a readable JWT") from err
    if not isinstance(payload, dict):
        raise ValueError("accessToken JWT payload is invalid")
    return payload


class RubetekPanelIntercomAPI(IntercomAPI):
    """Rubetek panel authorization/runtime profile.

    Panel-only identity is isolated here. The legacy phone/SMS IntercomAPI keeps
    its existing mobile device-token lifecycle and SignalR implementation.
    Shared REST endpoints and refresh-token handling remain in IntercomAPI;
    Panel-specific SIP session destruction stays isolated here.
    """

    def __init__(
        self,
        base_url: str = "https://api.domonap.ru",
        instance_id: Optional[str] = None,
        device_info: Optional[Any] = None,
    ) -> None:
        instance_id = instance_id or token_hex(8)
        if not _PANEL_INSTANCE_ID_RE.fullmatch(instance_id):
            raise ValueError("Rubetek panel instanceId must contain 16 hex digits")

        super().__init__(
            base_url=base_url,
            instance_id=instance_id.lower(),
            device_platform="panel",
            dom_app="panel",
        )

        # The prodAospRelease panel flow does not register an FCM/HMS token.
        self.device_token = None
        self.device_info = _normalize_panel_device_info(device_info, self.instance_id)
        self.headers["device-info"] = self.device_info
        self.panel: Dict[str, Any] = {}

    def signalr_headers(self) -> Dict[str, str]:
        """Headers supplied to the direct Microsoft SignalR WebSocket.

        dom-app/dom-platform, instanceId and device-info are REST headers. The
        SignalR access-token provider adds Authorization separately.
        """
        return {"User-Agent": SIGNALR_USER_AGENT}

    async def confirm_panel_authorization(
        self, confirm_code: str
    ) -> Dict[str, Any]:
        """Exchange the 8-digit provisioning code for a Panel session."""
        result = await self._post(
            "/sso-api/Authorization/ConfirmAuthorizationCode",
            {"confirmCode": confirm_code},
            need_auth=False,
            expect="json",
        )
        if isinstance(result, dict) and "error" in result and "status" in result:
            return result

        try:
            complete_token = result["completeToken"]
            self.panel = dict(result.get("panel") or {})
            self._install_panel_tokens(complete_token)
        except Exception as err:
            _LOGGER.exception("Unexpected panel authorization response: %s", err)
        return result

    def _install_panel_tokens(self, complete_token: Dict[str, Any]) -> None:
        access_token = complete_token["accessToken"]
        refresh_token = complete_token["refreshToken"]
        refresh_expiration = complete_token["refreshExpirationDate"]
        claims = _decode_jwt_payload(access_token)
        if claims.get(_PANEL_ROLE_CLAIM) != "Panel":
            raise ValueError("accessToken does not contain the Panel role")
        if self._parse_dt(refresh_expiration) is None:
            raise ValueError("refreshExpirationDate is invalid")

        self.set_tokens(access_token, refresh_token, refresh_expiration)
        if not self.panel.get("userId") and claims.get(_PANEL_NAME_CLAIM):
            self.panel["userId"] = claims[_PANEL_NAME_CLAIM]
        if self.token_update_callback:
            self.token_update_callback(
                access_token,
                refresh_token,
                refresh_expiration,
            )

    @classmethod
    def from_session_payload(
        cls,
        payload: str | Dict[str, Any],
        *,
        base_url: str = "https://api.domonap.ru",
    ) -> "RubetekPanelIntercomAPI":
        """Restore an already activated Panel session without consuming a code.

        Accepted JSON mirrors the captured activation exchange and adds the two
        request identity values needed to recreate the same client:

        {
          "instanceId": "0123456789abcdef",
          "deviceInfo": {...},
          "panel": {...},
          "completeToken": {...}
        }
        """
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as err:
                raise ValueError("panel session is not valid JSON") from err
        elif isinstance(payload, dict):
            data = dict(payload)
        else:
            raise ValueError("panel session must be a JSON object")

        instance_id = data.get("instanceId") or data.get("instance_id")
        if not isinstance(instance_id, str) or not _PANEL_INSTANCE_ID_RE.fullmatch(
            instance_id
        ):
            raise ValueError("panel session has no valid 16-hex instanceId")

        device_info = (
            data.get("deviceInfo")
            or data.get("device-info")
            or data.get("panel_device_info")
        )
        complete_token = data.get("completeToken") or data.get("complete_token")
        if not isinstance(complete_token, dict):
            raise ValueError("panel session has no completeToken object")

        api = cls(
            base_url=base_url,
            instance_id=instance_id,
            device_info=device_info,
        )
        panel = data.get("panel")
        if isinstance(panel, dict):
            api.panel = dict(panel)
        api._install_panel_tokens(complete_token)
        if not api.has_valid_refresh_token():
            raise ValueError("panel refresh token is missing or expired")
        return api

    def session_export(self) -> Dict[str, Any]:
        """Return the persistent non-code state needed to recreate this panel."""
        return {
            "instanceId": self.instance_id,
            "deviceInfo": json.loads(self.device_info),
            "panel": dict(self.panel),
            "completeToken": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "refreshExpirationDate": self.refresh_expiration_date,
            },
        }

    def start_active_sip_call(self, push_data: dict[str, Any]) -> None:
        """Start the temporary panel SIP account for an incoming call."""
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
            _LOGGER.debug("Incoming panel call does not contain complete SIP credentials")
            return
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid Domonap panel SIP port: %s", raw_port)
            return

        previous = self._active_sip_call
        if previous is not None:
            destroy = getattr(previous, "destroy", None)
            if callable(destroy):
                asyncio.create_task(
                    destroy(
                        timeout=1.5,
                        terminate_dialog=True,
                        reason="replaced_by_new_call",
                    )
                )
            else:
                asyncio.create_task(previous.stop())

        self._active_sip_call = RubetekPanelSipCall(
            str(account), str(password), str(domain), port
        )
        self._active_sip_call_id = call_id or self._active_call_id
        self._active_sip_call.start()

    async def open_relay_by_door_id(self, door_id: str):
        """Open a panel relay by resolving DoorId to the user's KeyId first.

        The Rubetek incoming-call UI answers the SIP call before requesting the
        relay opening, then invokes endCallSmart() after a successful open. That
        ordering matters for forked calls: rejecting our still-ringing branch
        does not stop the originating panel while accepting it does.
        """
        # Best effort only: a SIP problem must never prevent the requested door
        # from opening.
        await self._answer_active_sip_before_open()

        keys_response = await self.get_paged_keys()
        if not isinstance(keys_response, dict):
            return {
                "ok": False,
                "error": "Unexpected key list response",
                "body": str(keys_response),
            }
        if "error" in keys_response:
            return keys_response

        wanted_door_id = str(door_id)
        for key in keys_response.get("results", []):
            if not isinstance(key, dict):
                continue
            if str(key.get("doorId", "")) != wanted_door_id:
                continue
            key_id = key.get("id")
            if not key_id:
                return {
                    "ok": False,
                    "error": "Door key has no id",
                    "door_id": wanted_door_id,
                }
            _LOGGER.debug(
                "Panel relay DoorId=%s resolved to KeyId=%s",
                wanted_door_id,
                key_id,
            )
            return await self.open_relay_by_key_id(str(key_id))

        return {
            "ok": False,
            "error": "No panel key found for DoorId",
            "door_id": wanted_door_id,
        }

    async def end_call_notify(self, call_id: str) -> Dict[str, Any]:
        """Notify the Domonap backend that the active call has ended."""
        result = await self._post(
            "/communication-api/Call/NotifyCallEnded",
            {"callId": call_id},
            need_auth=True,
            expect="text",
        )
        if isinstance(result, dict) and "error" in result:
            return result
        _LOGGER.debug("Panel notifyCallEnded(%s) -> %s", call_id, result)
        return {"ok": True, "body": result}

    async def _safe_notify_call_ended(self, call_id: str) -> Dict[str, Any]:
        try:
            return await self.end_call_notify(call_id)
        except Exception as err:
            _LOGGER.warning("Panel NotifyCallEnded failed for %s: %s", call_id, err)
            return {"ok": False, "error": str(err)}

    async def _destroy_panel_sip_call(
        self,
        sip_call: Any,
        *,
        reason: str,
        terminate_dialog: bool = True,
    ) -> Dict[str, Any]:
        """Destroy one Panel SIP session, with compatibility for older test doubles."""
        destroy = getattr(sip_call, "destroy", None)
        if callable(destroy):
            return await destroy(
                timeout=2.0,
                terminate_dialog=terminate_dialog,
                reason=reason,
            )

        # Compatibility fallback for legacy/fake call objects. Runtime Panel
        # sessions always use RubetekPanelSipCall.destroy().
        sip_expected = bool(getattr(sip_call, "has_invite", False))
        if terminate_dialog and sip_expected:
            try:
                terminate_result = await sip_call.end(timeout=2.0)
            except Exception as err:
                terminate_result = {"ok": False, "error": str(err)}
        else:
            terminate_result = {
                "ok": False,
                "skipped": True,
                "reason": "no_sip_invite",
                "registered": getattr(sip_call, "registered", False),
            }
        try:
            await sip_call.stop()
        except Exception:
            _LOGGER.debug("Legacy Panel SIP stop failed", exc_info=True)
        return terminate_result

    async def destroy_active_sip_session(
        self,
        call_id: Optional[str] = None,
        *,
        reason: str = "signalr_call_ended",
        terminate_dialog: bool = True,
    ) -> Dict[str, Any] | None:
        """Destroy the temporary SIP account without sending REST call-end again."""
        async with self._active_call_lock:
            normalized = str(call_id).strip() if call_id is not None else ""
            if normalized and self._active_call_id and normalized != self._active_call_id:
                return None

            sip_call = self._active_sip_call
            self._active_sip_call = None
            self._active_sip_call_id = None
            if not normalized or self._active_call_id == normalized:
                self._active_call_id = None

            if sip_call is None:
                return {"ok": True, "skipped": True, "reason": "no_sip_session"}

            try:
                return await self._destroy_panel_sip_call(
                    sip_call,
                    reason=reason,
                    terminate_dialog=terminate_dialog,
                )
            except Exception as err:
                _LOGGER.warning("Panel SIP session destruction failed: %s", err)
                return {"ok": False, "error": str(err)}

    async def end_active_call(
        self, expected_call_id: Optional[str] = None
    ) -> Dict[str, Any] | None:
        """End the Panel call the way CallOrchestrator.endCallSmart() does.

        The APK posts NotifyCallEnded only while the temporary SIP account is
        not registered (sipRegState != Ok): REST is the fallback for a call that
        cannot be terminated over SIP. A registered session signals the end
        through BYE/reject inside ``destroy()`` and skips the REST notification.

        ``expected_call_id`` guards against overlapping calls: when a newer
        call already replaced the active one, nothing is torn down.
        """
        async with self._active_call_lock:
            call_id = self._active_call_id
            if not call_id:
                return None
            if expected_call_id is not None and call_id != expected_call_id:
                _LOGGER.debug(
                    "Active call moved from %s to %s; skipping stale teardown",
                    expected_call_id,
                    call_id,
                )
                return {"ok": True, "skipped": True, "reason": "call_replaced"}

            sip_call = self._active_sip_call
            sip_registered = bool(getattr(sip_call, "registered", False))
            sip_expected = sip_call is not None and bool(
                getattr(sip_call, "has_invite", False)
            )

            # CallOrchestrator launches notifyCallEnded in an IO coroutine and
            # immediately continues with SIP hangup/destroy. Mirror that: the
            # REST fallback must never delay terminating the SIP session.
            notify_task: Optional[asyncio.Task] = None
            if sip_call is None or not sip_registered:
                notify_task = asyncio.create_task(
                    self._safe_notify_call_ended(call_id),
                    name="domonap_panel_notify_call_ended",
                )

            if sip_call is not None:
                try:
                    sip_result = await self._destroy_panel_sip_call(
                        sip_call,
                        reason="local_call_end",
                        terminate_dialog=True,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Panel SIP session destruction failed for %s: %s",
                        call_id,
                        err,
                    )
                    sip_result = {"ok": False, "error": str(err)}
            else:
                sip_result = None

            # notifyCallEnded was only launched for an unregistered session.
            # Keep its result for diagnostics, but never use it to delay SIP
            # teardown.
            if notify_task is not None:
                notify_result = await notify_task
            else:
                notify_result = {
                    "ok": True,
                    "skipped": True,
                    "reason": "sip_registered",
                }

            notify_ok = (
                isinstance(notify_result, dict)
                and notify_result.get("ok") is True
            )
            sip_ok = isinstance(sip_result, dict) and sip_result.get("ok") is True
            ok = sip_ok if sip_expected else (sip_ok or notify_ok)

            # A new call may have replaced this session while the teardown was
            # running (double ring, call waiting). Only clear the state that
            # still belongs to the call being ended.
            if self._active_call_id == call_id:
                self._active_call_id = None
            if self._active_sip_call is sip_call:
                self._active_sip_call = None
                self._active_sip_call_id = None

            return {
                "ok": ok,
                "notify": notify_result,
                "sip": sip_result,
            }

    async def close(self):
        """Unload Panel runtime after unregistering its temporary SIP account."""
        if self._active_sip_call is not None:
            try:
                await self.destroy_active_sip_session(
                    reason="integration_unload",
                    terminate_dialog=True,
                )
            except Exception:
                _LOGGER.debug("Panel SIP cleanup on API close failed", exc_info=True)
        await super().close()

    async def logout(self) -> Dict[str, Any]:
        """Explicitly invalidate a panel session.

        Runtime unload/restart must not call this; refresh token persistence is
        identical to the regular API session lifecycle.
        """
        if not self.refresh_token:
            return {"ok": True, "skipped": True}
        refresh_token = self.refresh_token
        result = await self._post(
            "/sso-api/Authorization/Logout",
            {"refreshToken": refresh_token},
            need_auth=False,
            expect="text",
            retry_on_401=False,
        )
        if isinstance(result, dict) and "error" in result:
            return result
        self.set_tokens(None, None, None)
        if self.token_update_callback:
            self.token_update_callback(None, None, None)
        return {"ok": True, "body": result}
