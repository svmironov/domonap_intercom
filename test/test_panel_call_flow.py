import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from homeassistant.exceptions import HomeAssistantError

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
api_module = load_module("custom_components.domonap.api", "api.py")
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
load_module("custom_components.domonap.util", "util.py")
actions = load_module("custom_components.domonap.actions", "actions.py")

RubetekPanelIntercomAPI = panel_api.RubetekPanelIntercomAPI
PanelCallController = panel_call_controller.PanelCallController
RubetekPanelRuntimeConsumer = panel_runtime_consumer.RubetekPanelRuntimeConsumer


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def fire(self, event_type, data=None):
        self.events.append((event_type, data))


class FakeServiceRegistry:
    def __init__(self) -> None:
        self.registered = {}

    def async_register(self, domain, service, handler, schema=None, supports_response=False):
        self.registered[(domain, service)] = handler

    def async_remove(self, domain, service):
        self.registered.pop((domain, service), None)


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.services = FakeServiceRegistry()
        self.data = {}


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
    async def test_call_answered_elsewhere_ends_local_call(self):
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

    async def test_call_answered_elsewhere_keeps_established_call(self):
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

        async def fake_end_active_call(expected_call_id=None):
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

        async def fake_end_active_call(expected_call_id=None):
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

        async def fake_end_active_call(expected_call_id=None):
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


class SilenceRejectServiceTests(unittest.IsolatedAsyncioTestCase):
    def _setup_runtime(self, hass, api, controller):
        hass.data["domonap"] = {
            "entry-1": {
                "api": api,
                "call_controller": controller,
            }
        }

    async def test_silence_answers_then_ends_without_opening_the_door(self):
        """silence_active_call: answer (200 OK) -> end, no relay involved."""
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "silence_active_call")]

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        events = []

        class FakePanelSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True, "method": "sip_answer"}

        api._active_sip_call = FakePanelSipCall()

        async def fake_end_active_call(expected_call_id=None):
            events.append("end")
            return {"ok": True}

        api.end_active_call = fake_end_active_call
        controller = PanelCallController(
            hass, api, config_entry_id="entry-1", enabled=False
        )
        self._setup_runtime(hass, api, controller)

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["answered"])
        # Answer must happen before the teardown, so the 200 OK mutes the
        # panel even for this door-less path.
        self.assertEqual(events, ["answer", "end"])

    async def test_silence_works_for_phone_profile(self):
        """Phone/SMS entries answer their SIP session too, like the app."""
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "silence_active_call")]

        api = api_module.IntercomAPI(
            device_token="0123456789abcdef0123456789abcdef",
            instance_id="0123456789abcdef",
        )
        api.set_active_call("call-123")
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True, "method": "sip_answer"}

        api._active_sip_call = FakeSipCall()

        async def fake_end_active_call(expected_call_id=None):
            events.append("end")
            return {"ok": True}

        api.end_active_call = fake_end_active_call
        # No CALL_CONTROLLER in the runtime: that is the phone/SMS layout.
        hass.data["domonap"] = {"entry-1": {"api": api}}

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["answered"])
        self.assertEqual(events, ["answer", "end"])

    async def test_silence_skips_answer_when_external_call_established(self):
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "silence_active_call")]

        class EstablishedController:
            external_call_established = True

            async def end_call(self, *, source):
                return {"ok": True, "source": source}

        controller = EstablishedController()
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        answers = []

        async def recording_answer():
            answers.append("answer")
            return {"ok": True}

        api._answer_active_sip_before_open = recording_answer
        self._setup_runtime(hass, api, controller)

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["answered"])
        # The panel call is already answered here: a second 200 OK must not
        # be attempted on an established dialog.
        self.assertEqual(answers, [])

    async def test_reject_ends_without_answering(self):
        """reject_active_call: no 200 OK, straight to teardown (603 path)."""
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "reject_active_call")]

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        events = []

        async def fake_end_active_call(expected_call_id=None):
            events.append("end")
            return {"ok": True}

        api.end_active_call = fake_end_active_call
        answers = []

        async def recording_answer():
            answers.append("answer")
            return {"ok": True}

        api._answer_active_sip_before_open = recording_answer
        controller = PanelCallController(
            hass, api, config_entry_id="entry-1", enabled=False
        )
        self._setup_runtime(hass, api, controller)

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(events, ["end"])
        # Reject must never answer: a 200 OK would mute the panel, which is
        # exactly what this service must not do.
        self.assertEqual(answers, [])

    async def test_silence_without_active_call_returns_skipped(self):
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "silence_active_call")]

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        controller = PanelCallController(
            hass, api, config_entry_id="entry-1", enabled=False
        )
        self._setup_runtime(hass, api, controller)

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["call_id"])

    async def test_failed_relay_open_ends_already_answered_call(self):
        """A failed opening must not leave the muted call hanging (B3)."""
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "open_relay_by_door_id")]

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True, "method": "sip_answer"}

        api._active_sip_call = FakeSipCall()

        async def fake_post(path, payload=None, **kwargs):
            events.append("open_failed")
            return {"error": "HTTP 500", "status": 500}

        api._post = fake_post

        async def fake_end_active_call(expected_call_id=None):
            events.append("end")
            api.set_active_call(None)
            return {"ok": True}

        api.end_active_call = fake_end_active_call
        controller = PanelCallController(
            hass, api, config_entry_id="entry-1", enabled=False
        )
        self._setup_runtime(hass, api, controller)

        with self.assertRaises(HomeAssistantError):
            await handler(SimpleNamespace(data={"door_id": "door-1"}))

        self.assertEqual(
            events, ["answer", "open_failed", "end"]
        )
        self.assertIsNone(api.active_call_id)

    async def test_silence_answers_even_in_reject_mode(self):
        """silence_active_call mutes the panel regardless of call_end_mode."""
        hass = FakeHass()
        await actions.async_setup_actions(hass)
        handler = hass.services.registered[("domonap", "silence_active_call")]

        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.call_end_mode = "reject"
        api.set_active_call("call-123")
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True, "method": "sip_answer"}

        api._active_sip_call = FakeSipCall()

        async def fake_end_active_call(expected_call_id=None):
            events.append("end")
            return {"ok": True}

        api.end_active_call = fake_end_active_call
        controller = PanelCallController(
            hass, api, config_entry_id="entry-1", enabled=False
        )
        self._setup_runtime(hass, api, controller)

        result = await handler(SimpleNamespace(data={}))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["answered"])
        self.assertEqual(events, ["answer", "end"])


class ExternalForwardFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_failure_tears_down_call_without_errors(self):
        """A dial failure must clean up the call, not crash the forward task."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        ended = []

        async def fake_end_active_call(expected_call_id=None):
            ended.append(expected_call_id)
            api.set_active_call(None)
            return {"ok": True}

        api.end_active_call = fake_end_active_call

        controller = PanelCallController(
            FakeHass(), api, config_entry_id="entry-1", enabled=True
        )

        class FailingAccount:
            @property
            def active_call(self):
                return None

            async def dial(self, panel_call, *, call_id):
                raise RuntimeError("asterisk unreachable")

        controller._account = FailingAccount()

        class FakePanelSipCall:
            pass

        await controller._forward_to_external(FakePanelSipCall(), "call-123")

        self.assertEqual(ended, ["call-123"])
        self.assertIsNone(api.active_call_id)


if __name__ == "__main__":
    unittest.main()
