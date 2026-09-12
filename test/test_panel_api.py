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
load_module("custom_components.domonap.api", "api.py")
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

        original = panel_api.RubetekPanelSipCall
        panel_api.RubetekPanelSipCall = FakePanelSipCall
        try:
            result = await api.open_relay_by_door_id("door-1")
        finally:
            panel_api.RubetekPanelSipCall = original

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
