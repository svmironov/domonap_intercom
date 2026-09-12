import importlib.util
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


sip = load_module("custom_components.domonap.sip", "sip.py")
panel_sip = load_module("custom_components.domonap.panel_sip", "panel_sip.py")


class _DummyWriter:
    def close(self):
        pass

    async def wait_closed(self):
        pass


class PanelSipLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_sends_200_ok_with_no_media_sdp(self):
        """answer() accepts the INVITE with a discard-port SDP, like the app."""
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._local_host = "10.0.0.20"
        call._local_port = 51234
        call._invite = sip._SipMessage(
            "INVITE sip:user@sip.example SIP/2.0",
            {
                "via": ["SIP/2.0/TCP 10.0.0.1:5060;branch=br1"],
                "from": "<sip:panel@sip.example>;tag=remote",
                "to": "<sip:user@sip.example>",
                "call-id": ["call-1@sip.example"],
                "cseq": ["1 INVITE"],
            },
            (
                "v=0\r\n"
                "o=- 1 1 IN IP4 10.0.0.1\r\n"
                "s=Domonap\r\n"
                "c=IN IP4 10.0.0.1\r\n"
                "t=0 0\r\n"
                "m=audio 40000 RTP/AVP 8 0 101\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=recvonly\r\n"
            ).encode(),
        )
        call._invite_event.set()
        call._ack_event.set()
        sent = []

        async def fake_send(start_line, headers, body=b""):
            sent.append((start_line, list(headers), body))

        call._send = fake_send

        result = await call.answer(timeout=0.1)

        self.assertTrue(result["ok"])
        self.assertTrue(call.answered)
        self.assertEqual(len(sent), 1)
        start_line, headers, body = sent[0]
        self.assertEqual(start_line, "SIP/2.0 200 OK")
        self.assertIn("Content-Type: application/sdp", headers)
        sdp = body.decode()
        self.assertIn("m=audio 9 RTP/AVP 8 0 101", sdp)
        self.assertIn("a=recvonly", sdp)
        self.assertNotIn("40000", sdp)

    async def test_end_after_answer_sends_bye(self):
        """Hang-up after answering sends BYE, not 603 Decline."""
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._local_host = "10.0.0.20"
        call._local_port = 51234
        call._invite = sip._SipMessage(
            "INVITE sip:user@sip.example SIP/2.0",
            {
                "via": ["SIP/2.0/TCP 10.0.0.1:5060;branch=br2"],
                "from": "<sip:panel@sip.example>;tag=remote",
                "to": "<sip:user@sip.example>",
                "call-id": ["call-1@sip.example"],
                "cseq": ["1 INVITE"],
                "contact": ["<sip:panel@10.0.0.1:5060;transport=tcp>"],
            },
            b"",
        )
        call._invite_event.set()
        call._ack_event.set()
        call._answered = True
        sent = []

        async def fake_send(start_line, headers, body=b""):
            sent.append((start_line, list(headers)))
            if start_line.startswith("BYE "):
                cseq = next(
                    line.split(":", 1)[1].strip()
                    for line in headers
                    if line.lower().startswith("cseq:")
                )
                response = sip._SipMessage(
                    "SIP/2.0 200 OK",
                    {"cseq": [cseq]},
                    b"",
                )
                await call._handle_message(response)

        call._send = fake_send

        result = await call.end(timeout=0.5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "sip_bye")
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(sent), 1)
        start_line, headers = sent[0]
        self.assertTrue(start_line.startswith("BYE sip:panel@10.0.0.1:5060;transport=tcp "))
        self.assertTrue(any(h.startswith("Call-ID: call-1@sip.example") for h in headers))

    async def test_end_while_ringing_sends_decline(self):
        """Hang-up without answering still rejects with 603."""
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._local_host = "10.0.0.20"
        call._local_port = 51234
        call._invite = sip._SipMessage(
            "INVITE sip:user@sip.example SIP/2.0",
            {
                "via": ["SIP/2.0/TCP 10.0.0.1:5060;branch=br3"],
                "from": "<sip:panel@sip.example>;tag=remote",
                "to": "<sip:user@sip.example>",
                "call-id": ["call-1@sip.example"],
                "cseq": ["1 INVITE"],
            },
            b"",
        )
        call._invite_event.set()
        sent = []

        async def fake_send(start_line, headers, body=b""):
            sent.append((start_line, list(headers)))

        call._send = fake_send

        result = await call.end(timeout=0.5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "sip_decline")
        self.assertEqual(sent[0][0], "SIP/2.0 603 Decline")

    async def test_end_after_bye_returns_already_ended(self):
        """A second end() must not 603-decline an already-answered INVITE."""
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._local_host = "10.0.0.20"
        call._local_port = 51234
        call._invite = sip._SipMessage(
            "INVITE sip:user@sip.example SIP/2.0",
            {
                "via": ["SIP/2.0/TCP 10.0.0.1:5060;branch=br4"],
                "from": "<sip:panel@sip.example>;tag=remote",
                "to": "<sip:user@sip.example>",
                "call-id": ["call-1@sip.example"],
                "cseq": ["1 INVITE"],
                "contact": ["<sip:panel@10.0.0.1:5060;transport=tcp>"],
            },
            b"",
        )
        call._invite_event.set()
        call._ack_event.set()
        call._answered = True
        call._ended = True
        sent = []

        async def fake_send(start_line, headers, body=b""):
            sent.append(start_line)

        call._send = fake_send

        result = await call.end(timeout=0.1)

        self.assertTrue(result["ok"])
        self.assertTrue(result["already_ended"])
        self.assertEqual(sent, [])

    async def test_unregister_sends_zero_expiry_and_clears_registration(self):
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._registered_event.set()
        call._writer = _DummyWriter()
        call._local_host = "10.0.0.20"
        call._local_port = 51234
        sent = []

        async def fake_send(start_line, headers, body=b""):
            sent.append((start_line, list(headers), body))
            cseq = next(
                line.split(":", 1)[1].strip()
                for line in headers
                if line.lower().startswith("cseq:")
            )
            response = sip._SipMessage(
                "SIP/2.0 200 OK",
                {"cseq": [cseq]},
                b"",
            )
            await call._handle_message(response)

        call._send = fake_send
        result = await call.unregister(timeout=0.2)

        self.assertTrue(result["ok"])
        self.assertFalse(call.registered)
        self.assertEqual(len(sent), 1)
        start_line, headers, _ = sent[0]
        self.assertEqual(start_line, "REGISTER sip:sip.example:5060 SIP/2.0")
        self.assertIn("Expires: 0", headers)
        self.assertTrue(any(";expires=0" in header for header in headers))

    async def test_destroy_terminates_unregisters_then_stops(self):
        call = panel_sip.RubetekPanelSipCall("user", "secret", "sip.example", 5060)
        call._registered_event.set()
        call._invite = sip._SipMessage(
            "INVITE sip:user@sip.example SIP/2.0",
            {},
            b"",
        )
        order = []

        async def fake_end(timeout=2.0):
            order.append("end")
            call._ended = True
            return {"ok": True, "method": "sip_bye"}

        async def fake_unregister(timeout=2.0):
            order.append("unregister")
            call._registered_event.clear()
            return {"ok": True, "method": "sip_unregister"}

        async def fake_stop():
            order.append("stop")

        call.end = fake_end
        call.unregister = fake_unregister
        call.stop = fake_stop

        result = await call.destroy(reason="test")

        self.assertTrue(result["ok"])
        self.assertEqual(order, ["end", "unregister", "stop"])
        self.assertIsNone(call._invite)


if __name__ == "__main__":
    unittest.main()
