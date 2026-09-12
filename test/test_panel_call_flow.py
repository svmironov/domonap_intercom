import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "domonap"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components)

domonap_pkg = types.ModuleType("custom_components.domonap")
domonap_pkg.__path__ = [str(PKG)]
sys.modules.setdefault("custom_components.domonap", domonap_pkg)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PKG / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


load_module("custom_components.domonap.sip", "sip.py")
load_module("custom_components.domonap.api", "api.py")
panel_api = load_module("custom_components.domonap.panel_api", "panel_api.py")
load_module("custom_components.domonap.panel_sip", "panel_sip.py")
load_module(
    "custom_components.domonap.external_sip_signaling", "external_sip_signaling.py"
)
load_module("custom_components.domonap.panel_notify_consumer", "panel_notify_consumer.py")
panel_call_controller = load_module(
    "custom_components.domonap.panel_call_controller", "panel_call_controller.py"
)
panel_runtime_consumer = load_module(
    "custom_components.domonap.panel_runtime_consumer", "panel_runtime_consumer.py"
)

RubetekPanelIntercomAPI = panel_api.RubetekPanelIntercomAPI
PanelCallController = panel_call_controller.PanelCallController
RubetekPanelRuntimeConsumer = panel_runtime_consumer.RubetekPanelRuntimeConsumer


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def fire(self, event_type, data=None):
        self.events.append((event_type, data))


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()


class FakeController:
    def __init__(self, established: bool = False) -> None:
        self.established = established
        self.ended_calls = []

    @property
    def external_call_established(self) -> bool:
        return self.established

    async def on_panel_call_ended(self, call_id):
        self.ended_calls.append(call_id)


class PanelRuntimeConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_answered_elsewhere_tears_down_local_legs(self):
        """DomofonCallAnswered must silence a call that nobody accepted here.

        IncomingCallReceiver.onAnsweredByAnotherResident() runs
        CallOrchestrator.onCallAnsweredPush(), which calls endCallSmart()
        unless this device accepted the call itself.
        """
        hass = FakeHass()
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        notified = []

        async def fake_notify(call_id):
            notified.append(call_id)
            return {"ok": True, "body": ""}

        api.end_call_notify = fake_notify
        controller = FakeController(established=False)
        consumer = RubetekPanelRuntimeConsumer(
            hass,
            api,
            None,
            None,
            config_entry_id="entry-1",
            call_controller=controller,
        )

        await consumer._handle_invocation(
            {
                "target": "ReceivePush",
                "arguments": [
                    "",
                    "",
                    {
                        "EventMessage": "DomofonCallAnswered",
                        "CallId": "call-123",
                        "DoorId": "door-1",
                    },
                ],
            }
        )

        self.assertEqual(controller.ended_calls, ["call-123"])
        self.assertIsNone(api.active_call_id)
        # No SIP session existed, so endCallSmart()'s REST fallback fires.
        self.assertEqual(notified, ["call-123"])
        self.assertTrue(
            any(event_type == "domonap_call_answered" for event_type, _ in hass.bus.events)
        )

    async def test_call_answered_elsewhere_keeps_established_external_leg(self):
        """A locally accepted call survives the answered-elsewhere push."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")

        async def fake_notify(call_id):
            raise AssertionError("NotifyCallEnded must not fire for a live call")

        api.end_call_notify = fake_notify
        controller = FakeController(established=True)
        consumer = RubetekPanelRuntimeConsumer(
            FakeHass(),
            api,
            None,
            None,
            config_entry_id="entry-1",
            call_controller=controller,
        )

        await consumer._handle_invocation(
            {
                "target": "ReceivePush",
                "arguments": [
                    "",
                    "",
                    {
                        "EventMessage": "DomofonCallAnswered",
                        "CallId": "call-123",
                        "DoorId": "door-1",
                    },
                ],
            }
        )

        self.assertEqual(controller.ended_calls, [])
        self.assertEqual(api.active_call_id, "call-123")

    async def test_call_answered_elsewhere_skips_notify_for_newer_call(self):
        """A stale push must not touch or report the session of a newer call."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-new")
        notified = []

        async def fake_notify(call_id):
            notified.append(call_id)
            return {"ok": True, "body": ""}

        api.end_call_notify = fake_notify
        controller = FakeController(established=False)
        consumer = RubetekPanelRuntimeConsumer(
            FakeHass(),
            api,
            None,
            None,
            config_entry_id="entry-1",
            call_controller=controller,
        )

        await consumer._handle_invocation(
            {
                "target": "ReceivePush",
                "arguments": [
                    "",
                    "",
                    {
                        "EventMessage": "DomofonCallAnswered",
                        "CallId": "call-old",
                        "DoorId": "door-1",
                    },
                ],
            }
        )

        # The teardown itself is skipped by destroy_active_sip_session's
        # call-id guard, and the REST fallback must not fire either.
        self.assertEqual(controller.ended_calls, ["call-old"])
        self.assertEqual(notified, [])
        self.assertEqual(api.active_call_id, "call-new")


class PanelCallTimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_timer_ends_call_after_deadline(self):
        """EndCallTimer caps an unanswered call at the configured limit."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        ended = []

        async def fake_end_active_call():
            ended.append(time.monotonic())
            return {"ok": True}

        api.end_active_call = fake_end_active_call

        controller = PanelCallController(
            FakeHass(),
            api,
            config_entry_id="entry-1",
            enabled=False,
            call_timer_seconds=0.3,
        )
        started = time.monotonic()
        controller.on_incoming_call({"CallId": "call-1", "DoorId": "door-1"})

        await asyncio.sleep(0.9)

        self.assertEqual(len(ended), 1)
        self.assertGreaterEqual(ended[0] - started, 0.25)
        self.assertIsNone(controller._active_call_id)
        self.assertIsNone(controller._call_timer_task or None)

    async def test_call_timer_restarts_when_call_established(self):
        """The countdown restarts on SIP CONNECTED, mirroring observeSipEvents."""

        class EstablishedController(PanelCallController):
            def __init__(self, *args, established_after: float, **kwargs):
                super().__init__(*args, **kwargs)
                self._established_at = time.monotonic() + established_after

            @property
            def external_call_established(self) -> bool:
                return time.monotonic() >= self._established_at

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        ended = []

        async def fake_end_active_call():
            ended.append(time.monotonic())
            return {"ok": True}

        api.end_active_call = fake_end_active_call

        controller = EstablishedController(
            FakeHass(),
            api,
            config_entry_id="entry-1",
            enabled=False,
            call_timer_seconds=0.5,
            established_after=0.15,
        )
        started = time.monotonic()
        controller.on_incoming_call({"CallId": "call-1", "DoorId": "door-1"})

        await asyncio.sleep(1.4)

        self.assertEqual(len(ended), 1)
        # Without the restart the call would end at ~0.5s; with it the deadline
        # moves to ~0.65s (0.15s ringing + 0.5s connected).
        self.assertGreaterEqual(ended[0] - started, 0.55)

    async def test_call_timer_cancelled_when_call_ends(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        ended = []

        async def fake_end_active_call():
            ended.append(time.monotonic())
            return {"ok": True}

        api.end_active_call = fake_end_active_call

        controller = PanelCallController(
            FakeHass(),
            api,
            config_entry_id="entry-1",
            enabled=False,
            call_timer_seconds=0.3,
        )
        controller.on_incoming_call({"CallId": "call-1", "DoorId": "door-1"})
        await controller.on_panel_call_ended("call-1")

        await asyncio.sleep(0.6)

        self.assertEqual(ended, [])


if __name__ == "__main__":
    unittest.main()
