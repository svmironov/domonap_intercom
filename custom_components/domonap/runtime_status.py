"""Small, credential-free runtime status shared by diagnostics and entities."""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class RuntimeStatus:
    last_event_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def event_received(self):
        self.last_event_at = datetime.now(timezone.utc)

    def failed(self, component: str, reason: str):
        # Callers provide fixed codes or exception class names, never payloads.
        self.last_error = f"{component}: {reason}"
        self.last_error_at = datetime.now(timezone.utc)


def runtime_snapshot(runtime):
    api = runtime.get("api")
    if api is None:
        return {}
    consumer = runtime.get("notify_consumer")
    controller = runtime.get("call_controller")
    sip = getattr(api, "_active_sip_call", None)
    account = getattr(controller, "_account", None)
    return {
        "signalr": "connected" if consumer and consumer.connected else "disconnected",
        "sip": "idle" if sip is None else ("registered" if sip.registered else "unregistered"),
        "external_sip": "disabled" if not controller or not controller.enabled else (
            "registered" if account and account.registered else "unregistered"
        ),
        "last_event": api.runtime_status.last_event_at,
        "last_error": api.runtime_status.last_error,
        "last_error_at": api.runtime_status.last_error_at,
    }
