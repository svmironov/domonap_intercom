from __future__ import annotations

from typing import Any

from .panel_notify_consumer import RubetekPanelNotifyConsumer


class RubetekPanelRuntimeConsumer(RubetekPanelNotifyConsumer):
    """Entry-aware panel runtime layered on top of the captured transport.

    The direct SignalR transport stays isolated and unchanged. This wrapper only
    connects panel ReceivePush events to the shared IntercomAPI call lifecycle,
    tags events with their originating config entry and optionally hands call
    lifecycle events to the external SIP controller.
    """

    def __init__(
        self,
        *args,
        config_entry_id: str,
        call_controller=None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._config_entry_id = config_entry_id
        self._call_controller = call_controller

    async def _handle_invocation(self, data: dict[str, Any]) -> None:
        if data.get("target") == "ReceivePush":
            args = data.get("arguments") or []
            push_data = args[2] if len(args) >= 3 else None
            if isinstance(push_data, dict):
                push_data["config_entry_id"] = self._config_entry_id
                event_message = push_data.get("EventMessage")
                call_id = push_data.get("CallId") or push_data.get("callId")

                if event_message == "DomofonCalling":
                    self._api.set_active_call(call_id)
                    self._api.start_active_sip_call(push_data)
                    if self._call_controller is not None:
                        self._call_controller.on_incoming_call(push_data)
                elif event_message == "DomofonCallAnswered":
                    # Another resident answered the forked call. The APK runs
                    # endCallSmart() unless this device accepted the call itself
                    # (onCallAnsweredPush + isCallAccepted). The equivalent of a
                    # locally accepted call is an established external SIP dialog.
                    established = bool(
                        self._call_controller is not None
                        and self._call_controller.external_call_established
                    )
                    if not established:
                        if self._call_controller is not None:
                            await self._call_controller.on_panel_call_ended(call_id)
                        await self._destroy_panel_call(
                            call_id, reason="signalr_call_answered_elsewhere"
                        )
                elif event_message == "DomofonCallEnded":
                    if self._call_controller is not None:
                        await self._call_controller.on_panel_call_ended(call_id)
                    await self._destroy_panel_call(
                        call_id, reason="signalr_call_ended"
                    )

        await super()._handle_invocation(data)

    async def _destroy_panel_call(self, call_id, *, reason: str) -> None:
        """Destroy the panel SIP session with the endCallSmart() REST fallback."""
        api = self._api
        sip_call = getattr(api, "_active_sip_call", None)
        sip_registered = bool(getattr(sip_call, "registered", False))

        destroy = getattr(api, "destroy_active_sip_session", None)
        result = None
        if callable(destroy):
            result = await destroy(call_id, reason=reason, terminate_dialog=True)
        else:
            api.clear_active_call(call_id)

        # destroy was skipped because the session belongs to a newer call.
        if result is None:
            return

        # CallOrchestrator.endCallSmart() posts NotifyCallEnded while the
        # temporary SIP account is not registered: REST is then the only
        # end-of-call signal the backend receives.
        if not sip_registered and call_id:
            safe_notify = getattr(api, "_safe_notify_call_ended", None)
            if callable(safe_notify):
                await safe_notify(str(call_id))
