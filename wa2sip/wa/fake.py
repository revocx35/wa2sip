"""Simulated WhatsApp accounts (WA2SIP_WA_DRIVER=fake), for tests and trying out the PBX side.

The fake peer answers outgoing calls after `answer_delay` seconds and echoes back what it
hears (delayed by 0.3 s), so a PBX caller hears themselves. Some numbers misbehave on purpose:
ending in 0000 = declined, 1111 = busy, 2222 = never answers, 9999 = not on WhatsApp.
Incoming calls are injected with `simulate_incoming()` (API: POST /api/wa/{id}/simulate-call).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import secrets
from collections.abc import Callable

from ..models import digits_only
from .base import AudioPipe, WaCall, WaError, WaRuntime

log = logging.getLogger("wa2sip.wa.fake")

CONTACTS = [
    {"id": "491701111001@c.us", "number": "491701111001", "name": "Alice Example", "pushname": "Alice"},
    {"id": "491701111002@c.us", "number": "491701111002", "name": "Bob Example", "pushname": "Bob"},
    {"id": "905321111003@c.us", "number": "905321111003", "name": "Cem Örnek", "pushname": "Cem"},
    {"id": "123456789012345@lid", "number": "441632960004", "name": "Dana (LID)", "pushname": "Dana"},
]


class EchoPipe(AudioPipe):
    """The fake WhatsApp peer: repeats what it hears 0.3 s later."""

    def __init__(self, codec: str, on_audio: Callable[[bytes], None], delay_frames: int = 15):
        self.codec = codec
        self.on_audio = on_audio
        self.queue: collections.deque[bytes] = collections.deque()
        self.delay = delay_frames
        self.heard = 0       # bytes the fake peer got from us
        self.said = 0        # bytes it sent back
        self._task = asyncio.create_task(self._run())
        self.silence = bytes([0xD5 if codec == "PCMA" else 0xFF]) * 160

    def write(self, payload: bytes) -> None:
        self.heard += len(payload)
        self.queue.append(payload)
        while len(self.queue) > 100:
            self.queue.popleft()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        t = loop.time()
        while True:
            t += 0.02
            await asyncio.sleep(max(0.0, t - loop.time()))
            frame = self.queue.popleft() if len(self.queue) > self.delay else self.silence
            self.said += len(frame)
            self.on_audio(frame)

    async def close(self) -> None:
        self._task.cancel()

    def info(self) -> dict:
        # same meaning as PulsePipe: rx = from WhatsApp, tx = to WhatsApp
        return {"codec": self.codec, "rx_bytes": self.said, "tx_bytes": self.heard, "fake": True}


class FakeRuntime(WaRuntime):
    def __init__(self, account_id: str, name: str, answer_delay: float = 2.0, link_delay: float = 0.0):
        super().__init__(account_id, name)
        self.answer_delay = answer_delay
        self.link_delay = link_delay
        self._timers: list[asyncio.Task] = []

    async def start(self) -> None:
        self.set_state("loading")
        if self.link_delay:
            self.set_state("qr")
            self.qr = _FAKE_QR
            self._later(self.link_delay, self._linked)
        else:
            self._linked()

    def _linked(self) -> None:
        self.me = {"id": "490000000000@c.us", "number": "490000000000", "name": f"{self.name} (simulated)"}
        self.set_state("ready")

    async def stop(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers = []
        for c in list(self.calls.values()):
            c.state, c.reason = "ended", "stopped"
            self._emit_call(c)
        self.set_state("stopped")

    def _later(self, delay: float, fn) -> None:
        async def run():
            await asyncio.sleep(delay)
            fn()
        self._timers.append(asyncio.create_task(run()))

    def _need_ready(self) -> None:
        if self.state != "ready":
            raise WaError("not_ready", f"WhatsApp is not connected ({self.state})")

    async def contacts(self) -> list[dict]:
        self._need_ready()
        return [dict(c) for c in CONTACTS]

    async def lookup(self, number: str) -> dict | None:
        self._need_ready()
        n = digits_only(number)
        if not n or n.endswith("9999"):
            return None
        known = next((c for c in CONTACTS if c["number"] == n), None)
        return {"jid": known["id"] if known else f"{n}@c.us", "number": n, "name": known["name"] if known else ""}

    def _peer(self, target: str) -> dict:
        c = next((c for c in CONTACTS if target in (c["id"], c["number"])), None)
        if c:
            return {"jid": c["id"], "number": c["number"], "name": c["name"]}
        n = digits_only(target)
        return {"jid": f"{n}@c.us", "number": n, "name": ""}

    async def dial(self, target: str) -> WaCall:
        self._need_ready()
        if self.active_call():
            raise WaError("busy", "WhatsApp is already in a call")
        peer = self._peer(target)
        if peer["number"].endswith("9999"):
            raise WaError("not_on_whatsapp", "this number is not on WhatsApp")
        call = WaCall(id=secrets.token_hex(8).upper(), account_id=self.account_id, state="calling",
                      outgoing=True, peer=peer)
        self._emit_call(call)
        num = peer["number"]

        def ringing():
            if call.state == "calling":
                self._set(call, "ringing")
        self._later(0.3, ringing)
        if num.endswith("0000"):
            self._later(self.answer_delay, lambda: self._end(call, "declined"))
        elif num.endswith("1111"):
            self._later(0.5, lambda: self._end(call, "busy"))
        elif not num.endswith("2222"):
            self._later(self.answer_delay, lambda: call.state in ("calling", "ringing") and self._set(call, "active"))
        return call

    def _set(self, call: WaCall, state: str) -> None:
        call.state = state
        self._emit_call(call)

    def _end(self, call: WaCall, reason: str) -> None:
        if call.ended:
            return
        call.state, call.reason = "ended", reason
        self._emit_call(call)

    def simulate_incoming(self, number: str = "", name: str = "") -> WaCall:
        self._need_ready()
        peer = self._peer(number or CONTACTS[0]["number"])
        if name:
            peer["name"] = name
        call = WaCall(id=secrets.token_hex(8).upper(), account_id=self.account_id, state="incoming",
                      outgoing=False, peer=peer)
        self._emit_call(call)
        self._later(45, lambda: call.state == "incoming" and self._end(call, "missed"))
        return call

    def simulate_peer_hangup(self) -> None:
        call = self.active_call()
        if call:
            self._end(call, "hung up")

    def _get(self, call_id: str) -> WaCall:
        call = self.calls.get(call_id)
        if not call:
            raise WaError("no_such_call", "no such WhatsApp call")
        return call

    async def accept(self, call_id: str) -> None:
        call = self._get(call_id)
        if call.state != "incoming":
            raise WaError("bad_state", f"call is {call.state}")
        self._set(call, "connecting")
        self._later(0.2, lambda: call.state == "connecting" and self._set(call, "active"))

    async def reject(self, call_id: str) -> None:
        self._end(self._get(call_id), "declined")

    async def hangup(self, call_id: str) -> None:
        call = self.calls.get(call_id)
        if call:
            self._end(call, "hung up" if call.state == "active" else "cancelled")

    async def logout(self) -> None:
        self.me = None
        self.set_state("qr")
        self.qr = _FAKE_QR
        self._later(3, self._linked)

    async def request_pairing_code(self, phone: str) -> str:
        self.pairing_code = "FAKE-1234"
        self._later(3, self._linked)
        return self.pairing_code

    async def open_audio(self, codec: str, on_audio: Callable[[bytes], None]) -> AudioPipe:
        return EchoPipe(codec, on_audio)

    async def diagnostics(self) -> dict:
        return {"driver": "fake"}


# a 1x1 PNG: the UI shows *something* where the QR code goes
_FAKE_QR = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
