from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .const import EVENT_CALL_ENDED
from .external_sip_signaling import AsteriskSipAccount, ExternalSipConfig, parse_host_port
from .panel_sip import RubetekPanelSipCall

_LOGGER = logging.getLogger(__name__)

# EndCallTimer.TIMER_MILLIS = 0xea60 in the APK: the panel caps a call at 60
# seconds. The countdown starts when the call arrives (TelephonyService) and is
# restarted once the SIP call connects (CallOrchestrator.observeSipEvents), so
# neither ringing nor an established conversation outlives this limit.
CALL_TIMER_SECONDS = 60.0


class PanelCallController:
    """Coordinate Domonap Panel SIP, relay actions and an optional Asterisk dialog.

    The controller is intentionally outside IntercomAPI. Authentication and
    Domonap REST stay unchanged, while call routing policy can evolve without
    leaking Asterisk-specific behavior into phone/SMS mode.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: Any,
        *,
        config_entry_id: str,
        enabled: bool = False,
        user: str = "",
        password: str = "",
        domain: str = "",
        transport: str = "udp",
        call_number: str = "",
        call_timer_seconds: float = CALL_TIMER_SECONDS,
    ) -> None:
        self._hass = hass
        self._api = api
        self._config_entry_id = config_entry_id
        self._enabled = bool(enabled)
        self._user = user.strip()
        self._password = password
        self._domain = domain.strip()
        self._transport = transport.lower().strip() or "udp"
        self._call_number = call_number.strip()
        self._account: AsteriskSipAccount | None = None
        self._active_call_id: str | None = None
        self._active_door_id: str | None = None
        self._forward_task: asyncio.Task | None = None
        self._teardown_in_progress = False
        self._call_timer_seconds = float(call_timer_seconds)
        self._call_timer_tick = max(0.05, min(1.0, self._call_timer_seconds / 20.0))
        self._call_timer_task: asyncio.Task | None = None
        self._call_deadline: float | None = None
        self._call_timer_restarted_on_answer = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def external_call_active(self) -> bool:
        return bool(self._account and self._account.has_active_call)

    @property
    def external_call_established(self) -> bool:
        call = self._account.active_call if self._account else None
        return bool(call and call.established)

    async def start(self) -> None:
        if not self._enabled:
            return
        if not all((self._user, self._domain, self._call_number)):
            raise ValueError("External SIP requires user, domain and call number")
        host, port = parse_host_port(self._domain)
        config = ExternalSipConfig(
            enabled=True,
            user=self._user,
            password=self._password,
            host=host,
            port=port,
            transport=self._transport,
            call_number=self._call_number,
        )
        self._account = AsteriskSipAccount(
            config,
            on_dtmf=self._on_external_dtmf,
            on_hangup=self._on_external_hangup,
        )
        await self._account.start()

    async def stop(self) -> None:
        timer_task = self._call_timer_task
        self._cancel_call_timer()
        if timer_task is not None and not timer_task.done():
            try:
                await timer_task
            except asyncio.CancelledError:
                pass
        task = self._forward_task
        self._forward_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        account = self._account
        self._account = None
        if account is not None:
            await account.close()

    def on_incoming_call(self, push_data: dict[str, Any]) -> None:
        self._active_call_id = self._string_value(
            push_data.get("CallId") or push_data.get("callId")
        )
        self._active_door_id = self._string_value(
            push_data.get("DoorId") or push_data.get("doorId")
        )
        # The APK EndCallTimer runs for every incoming call, with or without a
        # forwarding target, so start it before the external SIP check.
        self._start_call_timer()
        if not self._enabled or self._account is None:
            return

        panel_call = getattr(self._api, "_active_sip_call", None)
        if not isinstance(panel_call, RubetekPanelSipCall):
            _LOGGER.warning("Cannot forward panel call: Domonap SIP session is unavailable")
            return

        previous = self._forward_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._forward_task = asyncio.create_task(
            self._forward_to_external(panel_call, self._active_call_id or ""),
            name="domonap_forward_to_external_sip",
        )

    async def on_panel_call_ended(self, call_id: str | None) -> None:
        normalized = self._string_value(call_id)
        if normalized and self._active_call_id and normalized != self._active_call_id:
            return
        self._cancel_call_timer()
        call = self._account.active_call if self._account else None
        if call is not None:
            try:
                await call.hangup(local=True)
            except Exception:
                _LOGGER.debug(
                    "Failed to close external SIP after Panel call ended",
                    exc_info=True,
                )
        self._active_call_id = None
        self._active_door_id = None

    async def open_door_by_door_id(self, door_id: str) -> Any:
        """Open a door using the APK-compatible call state transition.

        If the Asterisk dialog is already established, the Domonap SIP session
        is already answered and we only open the relay here. The caller must
        then invoke ``end_after_relay()`` so both SIP dialogs are terminated. If
        there is no established external dialog, the Panel API performs the
        APK-style silent answer before opening the relay.
        """
        if self.external_call_established:
            return await self._open_panel_relay_only(door_id)
        return await self._api.open_relay_by_door_id(door_id)

    async def open_door_by_key_id(self, key_id: str) -> Any:
        if not self.external_call_established:
            answer = getattr(self._api, "_answer_active_sip_before_open", None)
            if callable(answer):
                try:
                    await answer()
                except Exception:
                    _LOGGER.debug("Panel SIP pre-answer failed", exc_info=True)
        return await self._api.open_relay_by_key_id(key_id)

    def should_end_after_relay(self) -> bool:
        """Door opening always ends the call, matching the panel APK."""
        return True

    async def end_call(self, *, source: str = "manual") -> dict[str, Any] | None:
        """Terminate every active call dialog without touching the relay.

        Used by the silence/reject services: the Domonap SIP session and the
        optional external Asterisk dialog are ended together, exactly like the
        relay flow does after opening a door.
        """
        return await self._end_call_dialogs(source=source, end_external=True)

    async def end_after_relay(self, *, source: str = "relay") -> dict[str, Any] | None:
        """Terminate every active call dialog after a successful relay opening."""
        return await self.end_call(source=source)

    async def _forward_to_external(
        self, panel_call: RubetekPanelSipCall, call_id: str
    ) -> None:
        account = self._account
        if account is None:
            return
        try:
            await account.dial(panel_call, call_id=call_id)
            _LOGGER.info(
                "Forwarding Domonap call %s to external SIP number %s (signaling only)",
                call_id,
                self._call_number,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("Cannot start external SIP forwarding", exc_info=True)
            await self._end_call_dialogs(
                source="external_sip_forward_failure", end_external=False
            )

    async def _on_external_dtmf(self, digit: str) -> None:
        if digit != "1":
            _LOGGER.debug("Ignoring external SIP DTMF digit=%s", digit)
            return
        door_id = self._active_door_id
        if not door_id:
            _LOGGER.warning("DTMF 1 received but active Domonap door is unknown")
            return
        try:
            result = await self._open_panel_relay_only(door_id)
        except Exception:
            _LOGGER.exception("Failed to open Domonap relay on external SIP DTMF 1")
            return
        if not (isinstance(result, dict) and result.get("ok") is True):
            _LOGGER.error("External SIP DTMF 1 relay opening failed: %s", result)
            return

        _LOGGER.info(
            "Door %s opened by external SIP DTMF 1; ending the call",
            door_id,
        )
        end_result = await self._end_call_dialogs(
            source="external_sip_dtmf_1", end_external=True
        )
        if isinstance(end_result, dict) and not end_result.get("ok", False):
            _LOGGER.warning("Call teardown after DTMF 1 was incomplete: %s", end_result)

    async def _on_external_hangup(self) -> None:
        _LOGGER.info("External SIP call ended; terminating the Domonap call")
        await self._end_call_dialogs(
            source="external_sip_peer_hangup", end_external=False
        )

    def _start_call_timer(self) -> None:
        """Mirror the APK EndCallTimer: cap the current call at the time limit."""
        self._call_deadline = time.monotonic() + self._call_timer_seconds
        self._call_timer_restarted_on_answer = False
        if self._call_timer_task is None or self._call_timer_task.done():
            self._call_timer_task = asyncio.create_task(
                self._call_timer_loop(), name="domonap_panel_call_timer"
            )

    def _cancel_call_timer(self) -> None:
        self._call_deadline = None
        self._call_timer_restarted_on_answer = False
        task = self._call_timer_task
        self._call_timer_task = None
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    async def _call_timer_loop(self) -> None:
        """End the active call once the APK call deadline expires.

        The panel starts this countdown on the incoming push and restarts it
        when the SIP call connects. Establishment is observed by polling the
        external dialog state because the Asterisk bridge exposes no callback.
        """
        try:
            while True:
                await asyncio.sleep(self._call_timer_tick)
                if self._active_call_id is None and not self.external_call_active:
                    # No call in flight: nothing to time out. A later incoming
                    # push restarts the timer through _start_call_timer().
                    self._call_deadline = None
                    self._call_timer_restarted_on_answer = False
                    continue

                if (
                    self.external_call_established
                    and not self._call_timer_restarted_on_answer
                ):
                    # SIP CONNECTED restarts the countdown in the APK.
                    self._call_timer_restarted_on_answer = True
                    self._call_deadline = (
                        time.monotonic() + self._call_timer_seconds
                    )
                    _LOGGER.debug(
                        "Panel call timer restarted on established external dialog"
                    )

                deadline = self._call_deadline
                if deadline is not None and time.monotonic() >= deadline:
                    _LOGGER.info(
                        "Panel call %s reached the %.0fs limit; ending the call",
                        self._active_call_id,
                        self._call_timer_seconds,
                    )
                    await self._end_call_dialogs(
                        source="call_timer", end_external=True
                    )
                    return
        except asyncio.CancelledError:
            raise

    async def _end_call_dialogs(
        self,
        *,
        source: str,
        end_external: bool,
    ) -> dict[str, Any] | None:
        """End the Asterisk dialog and the temporary Domonap Panel session.

        The two SIP dialogs are independent. Once the door is opened they are
        terminated in parallel so neither keeps the call alive unnecessarily.
        ``RubetekPanelIntercomAPI.end_active_call`` is responsible for
        NotifyCallEnded + Panel SIP termination + REGISTER Expires: 0.
        """
        if self._teardown_in_progress:
            _LOGGER.debug("Call teardown already in progress (source=%s)", source)
            return {
                "ok": True,
                "skipped": True,
                "reason": "teardown_in_progress",
                "source": source,
            }

        call_id = getattr(self._api, "active_call_id", None) or self._active_call_id
        external_call = self._account.active_call if self._account else None
        if not call_id and (external_call is None or not end_external):
            return None

        self._teardown_in_progress = True
        try:
            external_task: asyncio.Task | None = None
            domonap_task: asyncio.Task | None = None

            if end_external and external_call is not None:
                external_task = asyncio.create_task(
                    external_call.hangup(local=True),
                    name="domonap_end_external_sip_dialog",
                )
            if call_id:
                domonap_task = asyncio.create_task(
                    self._api.end_active_call(expected_call_id=call_id),
                    name="domonap_end_panel_sip_call",
                )

            external_result: Any = None
            domonap_result: Any = None
            external_error: BaseException | None = None
            domonap_error: BaseException | None = None

            tasks = [task for task in (external_task, domonap_task) if task is not None]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                index = 0
                if external_task is not None:
                    value = results[index]
                    index += 1
                    if isinstance(value, BaseException):
                        external_error = value
                    else:
                        external_result = value
                if domonap_task is not None:
                    value = results[index]
                    if isinstance(value, BaseException):
                        domonap_error = value
                    else:
                        domonap_result = value

            external_ok = external_task is None or external_error is None
            domonap_ok = domonap_task is None or (
                domonap_error is None
                and isinstance(domonap_result, dict)
                and domonap_result.get("ok") is True
            )

            if external_error is not None:
                _LOGGER.warning(
                    "External SIP teardown failed after %s: %s",
                    source,
                    external_error,
                )
            elif external_task is not None:
                _LOGGER.info("External SIP call ended after %s", source)

            if domonap_error is not None:
                _LOGGER.warning(
                    "Domonap SIP teardown failed after %s: %s",
                    source,
                    domonap_error,
                )
            elif domonap_task is not None and domonap_ok:
                _LOGGER.info("Domonap call %s ended after %s", call_id, source)
                self._hass.bus.fire(
                    EVENT_CALL_ENDED,
                    {
                        "CallId": call_id,
                        "config_entry_id": self._config_entry_id,
                    },
                )
            elif domonap_task is not None:
                _LOGGER.warning(
                    "Domonap call teardown after %s was incomplete: %s",
                    source,
                    domonap_result,
                )

            ok = external_ok and domonap_ok
            if domonap_ok and self._active_call_id in (None, call_id):
                self._active_call_id = None
                self._active_door_id = None

            return {
                "ok": ok,
                "source": source,
                "external": (
                    {"ok": False, "error": str(external_error)}
                    if external_error is not None
                    else {"ok": True, "result": external_result}
                    if external_task is not None
                    else {"ok": True, "skipped": True}
                ),
                "domonap": (
                    {"ok": False, "error": str(domonap_error)}
                    if domonap_error is not None
                    else domonap_result
                    if domonap_task is not None
                    else {"ok": True, "skipped": True}
                ),
            }
        finally:
            self._teardown_in_progress = False
            # Do not cancel a call timer that a newer incoming call started
            # while this teardown was still running.
            if self._active_call_id in (None, call_id):
                self._cancel_call_timer()

    async def _open_panel_relay_only(self, door_id: str) -> Any:
        """Resolve DoorId -> KeyId without changing either SIP dialog."""
        keys_response = await self._api.get_paged_keys()
        if not isinstance(keys_response, dict):
            return {
                "ok": False,
                "error": "Unexpected key list response",
                "body": str(keys_response),
            }
        if "error" in keys_response:
            return keys_response

        wanted = str(door_id)
        for key in keys_response.get("results", []):
            if not isinstance(key, dict) or str(key.get("doorId", "")) != wanted:
                continue
            key_id = key.get("id")
            if not key_id:
                return {
                    "ok": False,
                    "error": "Door key has no id",
                    "door_id": wanted,
                }
            _LOGGER.debug(
                "External-call relay DoorId=%s resolved to KeyId=%s", wanted, key_id
            )
            return await self._api.open_relay_by_key_id(str(key_id))
        return {
            "ok": False,
            "error": "No panel key found for DoorId",
            "door_id": wanted,
        }

    @staticmethod
    def _string_value(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
