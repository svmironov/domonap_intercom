from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from secrets import token_hex
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)

_DIGEST_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')
_SIP_URI_RE = re.compile(r"<(sips?:[^>]+)>|(sips?:[^;,\s]+)", re.IGNORECASE)


@dataclass
class _SipMessage:
    start_line: str
    headers: dict[str, list[str]]
    body: bytes

    def first(self, name: str) -> str | None:
        normalized_name = name.lower()
        aliases = {
            "via": "v",
            "from": "f",
            "to": "t",
            "call-id": "i",
            "content-length": "l",
        }
        values = self.headers.get(normalized_name) or self.headers.get(
            aliases.get(normalized_name, "")
        )
        return values[0] if values else None


class DomonapSipCall:
    """SIP/TCP client for an incoming intercom call."""

    def __init__(self, account: str, password: str, domain: str, port: int) -> None:
        self._account = account
        self._password = password
        self._domain = domain
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._invite_event = asyncio.Event()
        self._registered_event = asyncio.Event()
        self._invite: _SipMessage | None = None
        self._stopping = False
        self._ended = False
        self._answered = False
        self._ack_event = asyncio.Event()
        self._bye_response_event = asyncio.Event()
        self._bye_response_status: int | None = None
        self._bye_cseq = 1
        self._local_host = "127.0.0.1"
        self._local_port = 5060
        self._register_call_id = f"{uuid4()}@home-assistant"
        self._from_tag = token_hex(8)
        self._to_tag = token_hex(8)
        self._cseq = 0
        self._nonce_count = 0
        self._expires = 300
        self._auth: dict[str, str] | None = None
        self._auth_header = "Authorization"
        self._register_response_event = asyncio.Event()
        self._register_response: _SipMessage | None = None
        self._pending_register_cseq: int | None = None
        self._destroy_lock = asyncio.Lock()
        self._destroyed = False

    @property
    def registered(self) -> bool:
        return self._registered_event.is_set()

    @property
    def has_invite(self) -> bool:
        return self._invite is not None

    @property
    def answered(self) -> bool:
        """Whether the incoming INVITE was accepted with a 200 OK."""
        return self._answered

    @property
    def sdp_offer(self) -> bytes:
        """Return the SDP body from the pending INVITE."""
        invite = self._invite
        return invite.body if invite is not None else b""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="domonap_sip_call")

    async def wait_for_invite(self, timeout: float = 8.0) -> bool:
        """Wait until an incoming INVITE is available."""
        return await self._wait_for_invite_event(timeout) and self._invite is not None

    async def _wait_for_invite_event(self, timeout: float) -> bool:
        self.start()
        try:
            await asyncio.wait_for(self._invite_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        self._stopping = True
        writer = self._writer
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        task = self._task
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def answer(
        self,
        timeout: float = 2.0,
        *,
        direction: str = "recvonly",
    ) -> dict[str, Any]:
        """Accept the pending INVITE without setting up an RTP endpoint.

        This reproduces the app's accept-without-talking flow: the 200 OK wins
        the forked call, so the intercom panel stops ringing, but the SDP
        advertises discard port 9 instead of opening a media socket in Home
        Assistant.
        """
        invite = await self._wait_invite(timeout)
        if isinstance(invite, dict):
            return invite
        if self._answered:
            return self._already_answered_result()
        if direction not in ("recvonly", "sendrecv", "inactive"):
            raise ValueError(f"Unsupported SIP media direction: {direction}")

        body = self._build_no_media_sdp_answer(invite, direction=direction)
        return await self._answer_with_body(invite, body, description=direction)

    async def answer_with_sdp(
        self,
        sdp_answer: bytes | str,
        *,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Accept the incoming INVITE with an external SDP answer unchanged.

        No RTP address, port, payload type or codec is rewritten here. The
        external endpoint is therefore the media endpoint visible to Domonap.
        """
        invite = await self._wait_invite(timeout)
        if isinstance(invite, dict):
            return invite
        if self._answered:
            return self._already_answered_result()

        body = sdp_answer.encode() if isinstance(sdp_answer, str) else bytes(sdp_answer)
        if not body:
            return {
                "ok": False,
                "error": "external_sip_answer_has_no_sdp",
                "registered": self.registered,
            }
        return await self._answer_with_body(
            invite,
            body,
            description="external-sdp-pass-through",
        )

    async def _wait_invite(self, timeout: float) -> _SipMessage | dict[str, Any]:
        if not await self._wait_for_invite_event(timeout):
            return {
                "ok": False,
                "error": "sip_invite_timeout",
                "registered": self.registered,
            }
        if self._invite is None:
            return {
                "ok": False,
                "error": "sip_invite_missing",
                "registered": self.registered,
            }
        return self._invite

    async def _answer_with_body(
        self,
        invite: _SipMessage,
        body: bytes,
        *,
        description: str,
    ) -> dict[str, Any]:
        await self._send_invite_ok(invite, body)
        self._answered = True
        _LOGGER.info(
            "Domonap SIP INVITE answered with 200 OK mode=%s",
            description,
        )

        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            _LOGGER.debug("SIP ACK not received before continuation")

        return {
            "ok": True,
            "method": "sip_answer",
            "ack": self._ack_event.is_set(),
            "mode": description,
        }

    def _already_answered_result(self) -> dict[str, Any]:
        return {
            "ok": True,
            "method": "sip_answer",
            "already_answered": True,
            "ack": self._ack_event.is_set(),
        }

    async def end(self, timeout: float = 5.0) -> dict[str, Any]:
        """End the call: BYE when answered, 603 Decline while still ringing.

        Mirrors the app: accept-then-hang-up sends BYE, declining a ringing
        call rejects only this branch with 603. A call that was already ended
        (our BYE or the remote side) reports success without new signaling:
        a UAS must not answer an INVITE with 3xx-6xx after a 2xx.
        """
        if self._answered:
            return await self._end_answered_call(timeout)
        return await self._reject_invite(timeout)

    async def _reject_invite(self, timeout: float) -> dict[str, Any]:
        if not await self._wait_for_invite_event(timeout):
            return {
                "ok": False,
                "error": "sip_invite_timeout",
                "registered": self.registered,
            }

        invite = self._invite
        if invite is None:
            return {"ok": self._ended, "registered": self.registered}

        try:
            await self._send_response(invite, 603, "Decline", add_to_tag=True)
            self._ended = True
            _LOGGER.info("Active Domonap call ended via SIP")
            return {"ok": True, "registered": self.registered, "method": "sip_decline"}
        except Exception as err:
            _LOGGER.warning("Failed to end Domonap call via SIP: %s", err)
            return {"ok": False, "error": str(err), "registered": self.registered}

    async def _end_answered_call(self, timeout: float) -> dict[str, Any]:
        if self._ended:
            return {
                "ok": True,
                "registered": self.registered,
                "method": "sip_bye",
                "already_ended": True,
            }

        if not self._ack_event.is_set():
            try:
                await asyncio.wait_for(
                    self._ack_event.wait(), timeout=min(timeout, 1.0)
                )
            except asyncio.TimeoutError:
                _LOGGER.warning("SIP ACK timeout; sending BYE anyway")

        try:
            await self._send_bye()
        except Exception as err:
            _LOGGER.warning("Failed to send SIP BYE: %s", err)
            return {
                "ok": False,
                "error": str(err),
                "registered": self.registered,
                "method": "sip_bye",
            }

        _LOGGER.info("Domonap SIP BYE sent")
        try:
            await asyncio.wait_for(self._bye_response_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "sip_bye_response_timeout",
                "registered": self.registered,
                "method": "sip_bye",
                "ack": self._ack_event.is_set(),
            }

        status = self._bye_response_status or 0
        ok = 200 <= status < 300
        if ok:
            self._ended = True
            _LOGGER.info("Domonap SIP BYE accepted with %s", status)
        else:
            _LOGGER.warning("Domonap SIP BYE failed with %s", status)
        return {
            "ok": ok,
            "status": status,
            "registered": self.registered,
            "method": "sip_bye",
            "ack": self._ack_event.is_set(),
        }

    async def unregister(self, timeout: float = 2.0) -> dict[str, Any]:
        """Remove the current SIP registration with ``Expires: 0``."""
        if not self.registered:
            return {
                "ok": True,
                "skipped": True,
                "reason": "not_registered",
                "method": "sip_unregister",
            }
        if self._writer is None:
            return {
                "ok": False,
                "error": "sip_connection_closed_before_unregister",
                "method": "sip_unregister",
            }

        response: _SipMessage | None = None
        for attempt in range(2):
            start_line, headers, cseq = self._build_register_request(expires=0)
            self._pending_register_cseq = cseq
            self._register_response = None
            self._register_response_event.clear()

            _LOGGER.info("Domonap SIP UNREGISTER sent (CSeq=%s)", cseq)
            await self._send(start_line, headers)
            try:
                await asyncio.wait_for(
                    self._register_response_event.wait(), timeout=timeout
                )
            except asyncio.TimeoutError:
                _LOGGER.warning("Domonap SIP UNREGISTER response timeout")
                return {
                    "ok": False,
                    "error": "sip_unregister_response_timeout",
                    "method": "sip_unregister",
                }

            response = self._register_response
            if response is None:
                return {
                    "ok": False,
                    "error": "sip_unregister_response_missing",
                    "method": "sip_unregister",
                }

            status = self._status_code(response)
            if status in (401, 407) and attempt == 0:
                try:
                    self._apply_auth_challenge(response)
                except RuntimeError:
                    return {
                        "ok": False,
                        "status": status,
                        "error": "sip_unregister_auth_challenge_missing",
                        "method": "sip_unregister",
                    }
                continue

            ok = 200 <= status < 300
            if ok:
                self._registered_event.clear()
                _LOGGER.info("Domonap SIP registration removed with %s", status)
            else:
                _LOGGER.warning("Domonap SIP UNREGISTER failed with %s", status)
            return {
                "ok": ok,
                "status": status,
                "method": "sip_unregister",
            }

        status = self._status_code(response) if response is not None else 0
        return {
            "ok": False,
            "status": status,
            "method": "sip_unregister",
        }

    async def destroy(
        self,
        *,
        timeout: float = 2.0,
        terminate_dialog: bool = True,
        reason: str = "call_end",
    ) -> dict[str, Any]:
        """Terminate the dialog, unregister the account and close SIP."""
        async with self._destroy_lock:
            if self._destroyed:
                return {
                    "ok": True,
                    "already_destroyed": True,
                    "method": "sip_destroy",
                }

            _LOGGER.info(
                "Destroying Domonap SIP session reason=%s invite=%s registered=%s",
                reason,
                self.has_invite,
                self.registered,
            )

            terminate_result: dict[str, Any] | None = None
            if terminate_dialog and self.has_invite and not self._ended:
                try:
                    terminate_result = await self.end(timeout=timeout)
                except Exception as err:
                    _LOGGER.warning("SIP dialog termination failed: %s", err)
                    terminate_result = {"ok": False, "error": str(err)}

            try:
                unregister_result = await self.unregister(timeout=timeout)
            except Exception as err:
                _LOGGER.warning("SIP unregister failed: %s", err)
                unregister_result = {"ok": False, "error": str(err)}

            await self.stop()
            self._destroyed = True
            self._invite = None
            self._invite_event.set()

            terminate_ok = (
                terminate_result is None
                or (
                    isinstance(terminate_result, dict)
                    and terminate_result.get("ok") is True
                )
            )
            unregister_ok = (
                isinstance(unregister_result, dict)
                and unregister_result.get("ok") is True
            )
            result = {
                "ok": terminate_ok and unregister_ok,
                "method": "sip_destroy",
                "terminate": terminate_result,
                "unregister": unregister_result,
            }
            _LOGGER.info(
                "Domonap SIP session destroyed terminate_ok=%s unregister_ok=%s",
                terminate_ok,
                unregister_ok,
            )
            return result

    async def _run(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._domain, self._port), timeout=5
            )
            sockname = self._writer.get_extra_info("sockname")
            if sockname:
                self._local_host = str(sockname[0])
                self._local_port = int(sockname[1])

            await self._register()
            while not self._stopping:
                message = await self._read_message()
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except (EOFError, ConnectionError):
            if not self._stopping:
                _LOGGER.debug("Domonap SIP connection closed")
        except Exception:
            if not self._stopping:
                _LOGGER.warning("Domonap SIP session failed", exc_info=True)
        finally:
            self._invite_event.set()
            writer = self._writer
            if writer is not None:
                writer.close()
            self._writer = None
            self._reader = None

    async def _register(self) -> None:
        await self._send_register()
        while True:
            response = await asyncio.wait_for(self._read_message(), timeout=5)
            if not response.start_line.startswith("SIP/2.0"):
                await self._handle_message(response)
                continue
            status = self._status_code(response)
            if status in (401, 407):
                self._apply_auth_challenge(response)
                await self._send_register()
                continue
            if 200 <= status < 300:
                self._registered_event.set()
                _LOGGER.info("Domonap SIP account registered")
                return
            if status == 423:
                min_expires = response.first("min-expires")
                try:
                    self._expires = max(self._expires, int(min_expires or ""))
                except ValueError:
                    raise RuntimeError("SIP registrar rejected registration expiry")
                await self._send_register()
                continue
            if status < 200:
                continue
            raise RuntimeError(f"SIP registration failed with {status}")

    async def _handle_message(self, message: _SipMessage) -> None:
        if message.start_line.startswith("SIP/2.0"):
            cseq = message.first("cseq") or ""
            cseq_upper = cseq.upper()
            if cseq_upper.endswith(" REGISTER"):
                try:
                    response_cseq = int(cseq.split(None, 1)[0])
                except (ValueError, IndexError):
                    response_cseq = -1
                if response_cseq == self._pending_register_cseq:
                    self._register_response = message
                    self._register_response_event.set()
            elif cseq_upper.endswith(" BYE"):
                self._bye_response_status = self._status_code(message)
                self._bye_response_event.set()
                _LOGGER.debug("SIP BYE response: %s", self._bye_response_status)
            return
        method = message.start_line.split(" ", 1)[0].upper()
        if method == "INVITE":
            self._invite = message
            await self._send_response(message, 100, "Trying")
            self._invite_event.set()
            _LOGGER.info("Incoming Domonap SIP INVITE received")
        elif method == "ACK":
            self._ack_event.set()
            _LOGGER.debug("SIP ACK received")
        elif method == "CANCEL":
            await self._send_response(message, 200, "OK", add_to_tag=True)
            if self._invite is not None:
                await self._send_response(
                    self._invite, 487, "Request Terminated", add_to_tag=True
                )
            self._ended = True
            self._invite = None
            self._invite_event.set()
        elif method == "BYE":
            await self._send_response(message, 200, "OK", add_to_tag=True)
            self._ended = True
            self._invite = None
            self._invite_event.set()
        elif method == "OPTIONS":
            await self._send_response(message, 200, "OK", add_to_tag=True)

    def _build_register_request(
        self, *, expires: int | None = None
    ) -> tuple[str, list[str], int]:
        """Build a REGISTER request shared by registration and teardown."""
        self._cseq += 1
        cseq = self._cseq
        expiry = self._expires if expires is None else expires
        branch = f"z9hG4bK{token_hex(12)}"
        host = self._format_host(self._local_host)
        request_uri = f"sip:{self._domain}:{self._port}"
        identity = f"sip:{self._account}@{self._domain}"
        contact = f"sip:{self._account}@{host}:{self._local_port};transport=tcp"
        headers = [
            f"Via: SIP/2.0/TCP {host}:{self._local_port};branch={branch};rport;alias",
            "Max-Forwards: 70",
            f"From: <{identity}>;tag={self._from_tag}",
            f"To: <{identity}>",
            f"Call-ID: {self._register_call_id}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: <{contact}>;expires={expiry}",
            f"Expires: {expiry}",
            "Supported: path, outbound, gruu",
            "User-Agent: Domonap Home Assistant",
        ]
        if self._auth is not None:
            authorization = self._digest_authorization("REGISTER", request_uri)
            headers.append(f"{self._auth_header}: {authorization}")
        return f"REGISTER {request_uri} SIP/2.0", headers, cseq

    async def _send_register(self) -> None:
        start_line, headers, _ = self._build_register_request()
        await self._send(start_line, headers)

    async def _send_response(
        self,
        request: _SipMessage,
        status: int,
        reason: str,
        *,
        add_to_tag: bool = False,
    ) -> None:
        headers = self._dialog_response_headers(request, add_to_tag=add_to_tag)
        headers.append("Server: Domonap Home Assistant")
        await self._send(f"SIP/2.0 {status} {reason}", headers)

    async def _send_invite_ok(self, invite: _SipMessage, body: bytes) -> None:
        headers = self._dialog_response_headers(invite, add_to_tag=True)
        host = self._format_host(self._local_host)
        headers.extend(
            [
                f"Contact: <sip:{self._account}@{host}:{self._local_port};transport=tcp>",
                "Content-Type: application/sdp",
                "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
                "Supported: replaces",
                "User-Agent: Domonap Home Assistant",
            ]
        )
        await self._send("SIP/2.0 200 OK", headers, body)

    async def _send_bye(self) -> None:
        invite = self._invite
        if invite is None:
            raise RuntimeError("No SIP INVITE available for BYE")

        remote_target = self._extract_uri(invite.first("contact"))
        if remote_target is None:
            remote_target = self._extract_uri(invite.first("from"))
        if remote_target is None:
            raise RuntimeError("Incoming SIP dialog has no remote target")

        local_to = invite.first("to")
        remote_from = invite.first("from")
        call_id = invite.first("call-id")
        if not local_to or not remote_from or not call_id:
            raise RuntimeError("Incoming SIP dialog identifiers are incomplete")
        if ";tag=" not in local_to.lower():
            local_to = f"{local_to};tag={self._to_tag}"

        host = self._format_host(self._local_host)
        branch = f"z9hG4bK{token_hex(12)}"
        headers = [
            f"Via: SIP/2.0/TCP {host}:{self._local_port};branch={branch};rport;alias",
            "Max-Forwards: 70",
            f"From: {local_to}",
            f"To: {remote_from}",
            f"Call-ID: {call_id}",
            f"CSeq: {self._bye_cseq} BYE",
            "User-Agent: Domonap Home Assistant",
        ]
        for route in invite.headers.get("record-route", []):
            headers.append(f"Route: {route}")

        self._bye_response_event.clear()
        self._bye_response_status = None
        await self._send(f"BYE {remote_target} SIP/2.0", headers)
        self._bye_cseq += 1

    def _build_no_media_sdp_answer(
        self,
        invite: _SipMessage,
        *,
        direction: str,
    ) -> bytes:
        """Build an audio answer using the discard port instead of an RTP socket."""
        offer = invite.body.decode("utf-8", errors="replace")
        lines = [
            line.strip()
            for line in offer.replace("\r", "").split("\n")
            if line.strip()
        ]
        host = self._local_host
        addr_type = "IP6" if ":" in host else "IP4"
        stamp = int(time.time() * 1000)
        answer = [
            "v=0",
            f"o=- {stamp} {stamp} IN {addr_type} {host}",
            "s=Domonap",
            f"c=IN {addr_type} {host}",
            "t=0 0",
        ]

        sections: list[list[str]] = []
        current: list[str] | None = None
        for line in lines:
            if line.startswith("m="):
                current = [line]
                sections.append(current)
            elif current is not None:
                current.append(line)

        audio_accepted = False
        for section in sections:
            parts = section[0][2:].split()
            if len(parts) < 4:
                continue
            media, _remote_port, proto, *formats = parts
            plain_rtp = proto.upper() in ("RTP/AVP", "RTP/AVPF")
            if media.lower() == "audio" and plain_rtp and not audio_accepted:
                answer.append(f"m=audio 9 {proto} {' '.join(formats)}")
                answer.append(f"c=IN {addr_type} {host}")
                for attribute in section[1:]:
                    lower = attribute.lower()
                    if lower.startswith(("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:")):
                        answer.append(attribute)
                answer.append(f"a={direction}")
                audio_accepted = True
            else:
                answer.append(f"m={media} 0 {proto} {' '.join(formats)}")

        if not sections:
            _LOGGER.warning("Incoming SIP INVITE has no SDP media sections")
        elif not audio_accepted:
            _LOGGER.warning("SIP offer has no supported plain RTP audio stream")

        return ("\r\n".join(answer) + "\r\n").encode("utf-8")

    @staticmethod
    def _extract_uri(value: str | None) -> str | None:
        if not value:
            return None
        match = _SIP_URI_RE.search(value)
        if not match:
            return None
        return match.group(1) or match.group(2)

    def _dialog_response_headers(
        self, request: _SipMessage, *, add_to_tag: bool
    ) -> list[str]:
        """Copy the headers that identify a SIP dialog into a response."""
        headers: list[str] = []
        for via in request.headers.get("via", []) or request.headers.get("v", []):
            headers.append(f"Via: {via}")
        for name in ("from", "to", "call-id", "cseq"):
            value = request.first(name)
            if value is None:
                continue
            if name == "to" and add_to_tag and ";tag=" not in value.lower():
                value = f"{value};tag={self._to_tag}"
            headers.append(f"{self._display_header(name)}: {value}")
        return headers

    async def _send(self, start_line: str, headers: list[str], body: bytes = b"") -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("SIP connection is not open")
        packet = (
            start_line
            + "\r\n"
            + "\r\n".join(headers)
            + f"\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        async with self._write_lock:
            writer.write(packet)
            await writer.drain()

    async def _read_message(self) -> _SipMessage:
        reader = self._reader
        if reader is None:
            raise ConnectionError("SIP connection is not open")
        while True:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            while raw_headers.startswith(b"\r\n"):
                raw_headers = raw_headers[2:]
            if not raw_headers:
                continue
            break
        lines = raw_headers[:-4].decode(errors="replace").split("\r\n")
        start_line = lines[0]
        headers: dict[str, list[str]] = {}
        current_name: str | None = None
        for line in lines[1:]:
            if line[:1] in (" ", "\t") and current_name:
                headers[current_name][-1] += " " + line.strip()
                continue
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            current_name = name.strip().lower()
            headers.setdefault(current_name, []).append(value.strip())
        length_value = (headers.get("content-length") or headers.get("l") or ["0"])[0]
        try:
            length = int(length_value)
        except ValueError:
            length = 0
        body = await reader.readexactly(length) if length else b""
        return _SipMessage(start_line, headers, body)

    def _digest_authorization(self, method: str, uri: str) -> str:
        auth = self._auth or {}
        realm = auth.get("realm", "")
        nonce = auth.get("nonce", "")
        algorithm = auth.get("algorithm", "MD5").upper()
        if algorithm not in ("MD5", "MD5-SESS"):
            raise RuntimeError(f"Unsupported SIP digest algorithm {algorithm}")
        cnonce = token_hex(8)
        self._nonce_count += 1
        nc = f"{self._nonce_count:08x}"
        ha1 = self._md5(f"{self._account}:{realm}:{self._password}")
        if algorithm == "MD5-SESS":
            ha1 = self._md5(f"{ha1}:{nonce}:{cnonce}")
        ha2 = self._md5(f"{method}:{uri}")
        qop_values = [value.strip() for value in auth.get("qop", "").split(",")]
        qop = "auth" if "auth" in qop_values else ""
        response = (
            self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            if qop
            else self._md5(f"{ha1}:{nonce}:{ha2}")
        )
        values = [
            f'username="{self._account}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{response}"',
            f"algorithm={algorithm}",
        ]
        if qop:
            values.extend((f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'))
        if opaque := auth.get("opaque"):
            values.append(f'opaque="{opaque}"')
        return "Digest " + ", ".join(values)

    def _apply_auth_challenge(self, response: _SipMessage) -> None:
        """Update digest state from a 401 or 407 response."""
        status = self._status_code(response)
        challenge_name = (
            "www-authenticate" if status == 401 else "proxy-authenticate"
        )
        challenge = response.first(challenge_name)
        if not challenge:
            raise RuntimeError("SIP authentication challenge is missing")
        self._auth = self._parse_digest(challenge)
        self._auth_header = (
            "Authorization" if status == 401 else "Proxy-Authorization"
        )

    @staticmethod
    def _parse_digest(value: str) -> dict[str, str]:
        if value.lower().startswith("digest "):
            value = value[7:]
        return {
            match.group(1).lower(): match.group(2) or match.group(3) or ""
            for match in _DIGEST_PARAM_RE.finditer(value)
        }

    @staticmethod
    def _md5(value: str) -> str:
        return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()

    @staticmethod
    def _status_code(message: _SipMessage) -> int:
        try:
            return int(message.start_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            return 0

    @staticmethod
    def _format_host(host: str) -> str:
        return f"[{host}]" if ":" in host and not host.startswith("[") else host

    @staticmethod
    def _display_header(name: str) -> str:
        return {"call-id": "Call-ID", "cseq": "CSeq"}.get(name, name.title())
