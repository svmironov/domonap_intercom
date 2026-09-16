import base64
import importlib.util
import json
import sys
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
api_module = load_module("custom_components.domonap.api", "api.py")
panel_api = load_module("custom_components.domonap.panel_api", "panel_api.py")
RubetekPanelIntercomAPI = panel_api.RubetekPanelIntercomAPI

ROLE_CLAIM = "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
NAME_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"


def fake_jwt(role="Panel", user_id="panel-user"):
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {ROLE_CLAIM: role, NAME_CLAIM: user_id}

    def enc(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc(header)}.{enc(payload)}.signature"


class RubetekPanelApiTests(unittest.IsolatedAsyncioTestCase):
    def test_panel_identity_matches_captured_aosp_contract(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        self.assertEqual(api.headers["dom-app"], "panel;")
        self.assertEqual(api.headers["dom-platform"], "panel;")
        self.assertEqual(api.headers["instanceId"], "0123456789abcdef")
        self.assertIsNone(api.device_token)

        info = json.loads(api.headers["device-info"])
        self.assertEqual(info["InstanceId"], "0123456789abcdef")
        self.assertEqual(info["versionCode"], "9845")
        self.assertEqual(info["versionName"], "9845")
        self.assertIn("Brand", info)
        self.assertNotIn("brand", info)

        signalr = api.signalr_headers()
        self.assertIn("User-Agent", signalr)
        self.assertNotIn("dom-app", signalr)
        self.assertNotIn("dom-platform", signalr)
        self.assertNotIn("instanceId", signalr)
        self.assertNotIn("device-info", signalr)

    async def test_activation_code_uses_panel_endpoint_and_stores_session(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        calls = []

        async def fake_post(path, payload=None, **kwargs):
            calls.append((path, payload, kwargs))
            return {
                "panel": {"userId": "panel-user", "name": "Test Panel"},
                "completeToken": {
                    "accessToken": fake_jwt(),
                    "refreshToken": "refresh-token",
                    "expirationDate": "2098-01-01T00:00:00Z",
                    "refreshExpirationDate": "2099-01-01T00:00:00Z",
                },
            }

        api._post = fake_post
        result = await api.confirm_panel_authorization("12345678")

        self.assertIn("completeToken", result)
        self.assertEqual(calls[0][0], "/sso-api/Authorization/ConfirmAuthorizationCode")
        self.assertEqual(calls[0][1], {"confirmCode": "12345678"})
        self.assertFalse(calls[0][2]["need_auth"])
        self.assertEqual(api.panel["userId"], "panel-user")
        self.assertEqual(api.refresh_token, "refresh-token")

    async def test_panel_open_by_door_id_resolves_key_id(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        opened = []

        async def fake_get_paged_keys(*args, **kwargs):
            return {
                "results": [
                    {"id": "key-other", "doorId": "door-other"},
                    {"id": "key-target", "doorId": "door-target"},
                ]
            }

        async def fake_open_by_key_id(key_id):
            opened.append(key_id)
            return {"ok": True, "body": ""}

        api.get_paged_keys = fake_get_paged_keys
        api.open_relay_by_key_id = fake_open_by_key_id

        result = await api.open_relay_by_door_id("door-target")

        self.assertEqual(result["ok"], True)
        self.assertEqual(opened, ["key-target"])

    async def test_panel_open_by_unknown_door_does_not_call_relay(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        opened = []

        async def fake_get_paged_keys(*args, **kwargs):
            return {"results": [{"id": "key-other", "doorId": "door-other"}]}

        async def fake_open_by_key_id(key_id):
            opened.append(key_id)
            return {"ok": True}

        api.get_paged_keys = fake_get_paged_keys
        api.open_relay_by_key_id = fake_open_by_key_id

        result = await api.open_relay_by_door_id("door-target")

        self.assertEqual(result["ok"], False)
        self.assertEqual(opened, [])

    async def test_phone_open_by_door_id_answers_sip_before_relay(self):
        """The phone profile mutes the panel the same way the app does.

        The base IntercomAPI answers its SIP session before the relay REST
        call: 200 OK wins the forked call, the panel stops ringing, and no
        media ever flows through Home Assistant.
        """
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

        async def fake_post(path, payload=None, **kwargs):
            events.append(("open", path, payload))
            return ""

        api._post = fake_post

        result = await api.open_relay_by_door_id("door-9")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["answer", ("open", "/client-api/Device/OpenRelayByDoorId", {"doorId": "door-9"})])

    async def test_phone_open_by_key_id_answers_sip_before_relay(self):
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

        async def fake_post(path, payload=None, **kwargs):
            events.append(("open", path))
            return ""

        api._post = fake_post

        result = await api.open_relay_by_key_id("key-9")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["answer", ("open", "/client-api/Device/OpenRelayByKeyId")])

    async def test_phone_open_relay_without_active_call_skips_answer(self):
        """No incoming call: the relay must open without touching SIP."""
        api = api_module.IntercomAPI(
            device_token="0123456789abcdef0123456789abcdef",
            instance_id="0123456789abcdef",
        )
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True}

        api._active_sip_call = FakeSipCall()

        async def fake_post(path, payload=None, **kwargs):
            events.append("open")
            return ""

        api._post = fake_post

        result = await api.open_relay_by_door_id("door-9")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["open"])

    async def test_panel_end_active_call_skips_stale_call_id(self):
        """A teardown for an old call must not touch the replacement call."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-new")
        destroyed = []

        class FakeSipCall:
            has_invite = True
            registered = True

            async def destroy(self, **kwargs):
                destroyed.append("destroy")
                return {"ok": True, "method": "sip_destroy"}

        sip_call = FakeSipCall()
        api._active_sip_call = sip_call

        result = await api.end_active_call(expected_call_id="call-old")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "call_replaced")
        self.assertEqual(destroyed, [])
        self.assertEqual(api.active_call_id, "call-new")
        self.assertIs(api._active_sip_call, sip_call)

    async def test_reject_mode_skips_answer_before_relay(self):
        """call_end_mode=reject opens the door without accepting the call."""
        api = api_module.IntercomAPI(
            device_token="0123456789abcdef0123456789abcdef",
            instance_id="0123456789abcdef",
        )
        api.call_end_mode = "reject"
        api.set_active_call("call-123")
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True}

        api._active_sip_call = FakeSipCall()

        async def fake_post(path, payload=None, **kwargs):
            events.append("open")
            return ""

        api._post = fake_post

        result = await api.open_relay_by_door_id("door-9")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["open"])

    async def test_reject_mode_still_allows_forced_answer(self):
        """The silence service forces the answer regardless of the mode."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.call_end_mode = "reject"
        api.set_active_call("call-123")
        events = []

        class FakeSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True}

        api._active_sip_call = FakeSipCall()

        result = await api._answer_active_sip_before_open(force=True)

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["answer"])

    async def test_panel_notify_call_ended_matches_apk_contract(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        calls = []

        async def fake_post(path, payload=None, **kwargs):
            calls.append((path, payload, kwargs))
            return ""

        api._post = fake_post
        result = await api.end_call_notify("call-123")

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "/communication-api/Call/NotifyCallEnded")
        self.assertEqual(calls[0][1], {"callId": "call-123"})
        self.assertTrue(calls[0][2]["need_auth"])
        self.assertEqual(calls[0][2]["expect"], "text")

    async def test_panel_open_by_door_id_answers_sip_before_relay(self):
        """The panel goes silent when the call is answered BEFORE the relay opens.

        CallOrchestrator.openDoorSilentlyAndEndCall() runs answer() first: the
        SIP 200 OK wins the forked call and stops the intercom from ringing.
        """
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        events = []

        class FakePanelSipCall:
            async def answer(self, timeout=2.0, **kwargs):
                events.append("answer")
                return {"ok": True, "method": "sip_answer"}

        api._active_sip_call = FakePanelSipCall()

        async def fake_get_paged_keys(*args, **kwargs):
            return {"results": [{"id": "key-1", "doorId": "door-1"}]}

        async def fake_open_by_key_id(key_id):
            events.append("open")
            return {"ok": True, "body": ""}

        api.get_paged_keys = fake_get_paged_keys
        api.open_relay_by_key_id = fake_open_by_key_id

        result = await api.open_relay_by_door_id("door-1")

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["answer", "open"])

    async def test_panel_end_active_call_skips_notify_when_sip_registered(self):
        """endCallSmart() posts NotifyCallEnded only when sipRegState != Ok."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        notified = []

        class FakeSipCall:
            has_invite = True
            registered = True

            async def destroy(self, **kwargs):
                return {"ok": True, "method": "sip_destroy"}

        async def fake_notify(call_id):
            notified.append(call_id)
            return {"ok": True, "body": ""}

        api._active_sip_call = FakeSipCall()
        api.end_call_notify = fake_notify

        result = await api.end_active_call()

        self.assertTrue(result["ok"])
        self.assertEqual(notified, [])
        self.assertTrue(result["notify"]["skipped"])
        self.assertEqual(result["notify"]["reason"], "sip_registered")
        self.assertTrue(result["sip"]["ok"])
        self.assertIsNone(api.active_call_id)

    async def test_panel_end_active_call_notifies_backend_when_sip_not_registered(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")
        notified = []

        class FakeSipCall:
            has_invite = False
            registered = False

            async def destroy(self, **kwargs):
                return {"ok": True, "method": "sip_destroy"}

        async def fake_notify(call_id):
            notified.append(call_id)
            return {"ok": True, "body": ""}

        api._active_sip_call = FakeSipCall()
        api.end_call_notify = fake_notify

        result = await api.end_active_call()

        self.assertTrue(result["ok"])
        self.assertEqual(notified, ["call-123"])
        self.assertTrue(result["notify"]["ok"])
        self.assertIsNone(api.active_call_id)

    async def test_panel_end_active_call_keeps_replacement_call_state(self):
        """A new call arriving during teardown must not be wiped (race guard)."""
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-old")
        notified = []

        class FakeSipCall:
            has_invite = True
            registered = False

            def __init__(self):
                self.destroyed = False

            async def destroy(self, **kwargs):
                self.destroyed = True
                # A new call lands while the old one is being torn down.
                api.set_active_call("call-new")
                return {"ok": True, "method": "sip_destroy"}

        old_call = FakeSipCall()
        new_call = FakeSipCall()
        api._active_sip_call = old_call

        async def fake_notify(call_id):
            notified.append(call_id)
            return {"ok": True, "body": ""}

        api.end_call_notify = fake_notify

        # Simulate start_active_sip_call replacing the session mid-teardown.
        original_destroy = api._destroy_panel_sip_call

        async def destroy_with_replacement(sip_call, **kwargs):
            result = await original_destroy(sip_call, **kwargs)
            api._active_sip_call = new_call
            api._active_sip_call_id = "call-new"
            api._active_call_id = "call-new"
            return result

        api._destroy_panel_sip_call = destroy_with_replacement

        result = await api.end_active_call()

        self.assertTrue(result["ok"])
        self.assertTrue(old_call.destroyed)
        self.assertFalse(new_call.destroyed)
        # The replacement call keeps its state: only the old call was reported.
        self.assertEqual(notified, ["call-old"])
        self.assertEqual(api.active_call_id, "call-new")
        self.assertIs(api._active_sip_call, new_call)

    async def test_panel_end_active_call_does_not_wait_for_missing_invite(self):
        api = RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call-123")

        class FakeSipCall:
            has_invite = False
            registered = True

            async def end(self, timeout=5.0):
                raise AssertionError("SIP end must not be awaited without INVITE")

            async def stop(self):
                return None

        async def fake_notify(call_id):
            return {"ok": True, "body": ""}

        api._active_sip_call = FakeSipCall()
        api.end_call_notify = fake_notify

        result = await api.end_active_call()

        self.assertTrue(result["ok"])
        self.assertTrue(result["notify"]["skipped"])
        self.assertEqual(result["sip"]["reason"], "no_sip_invite")
        self.assertIsNone(api.active_call_id)

    def test_existing_session_import_restores_exact_identity(self):
        session = {
            "instanceId": "0123456789abcdef",
            "deviceInfo": {
                "Brand": "Android",
                "Device": "emulator64_x86_64",
                "ID": "SE1B.240122.005",
                "InstanceId": "0123456789abcdef",
                "Manufacturer": "unknown",
                "Model": "Android SDK built for x86_64",
                "OsVersion": "kernel",
                "Product": "sdk_phone64_x86_64",
                "Release": "12",
                "versionCode": "9845",
                "versionName": "9845",
            },
            "panel": {"userId": "panel-user", "name": "Test Panel"},
            "completeToken": {
                "accessToken": fake_jwt(),
                "refreshToken": "refresh-token",
                "refreshExpirationDate": "2099-01-01T00:00:00Z",
            },
        }

        api = RubetekPanelIntercomAPI.from_session_payload(json.dumps(session))
        self.assertEqual(api.instance_id, "0123456789abcdef")
        self.assertEqual(json.loads(api.device_info)["OsVersion"], "kernel")
        self.assertEqual(api.panel["name"], "Test Panel")
        self.assertEqual(api.access_token, session["completeToken"]["accessToken"])

    def test_existing_session_rejects_non_panel_jwt(self):
        session = {
            "instanceId": "0123456789abcdef",
            "completeToken": {
                "accessToken": fake_jwt(role="User"),
                "refreshToken": "refresh-token",
                "refreshExpirationDate": "2099-01-01T00:00:00Z",
            },
        }
        with self.assertRaises(ValueError):
            RubetekPanelIntercomAPI.from_session_payload(session)


if __name__ == "__main__":
    unittest.main()
