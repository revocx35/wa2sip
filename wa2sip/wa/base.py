"""What the engine needs from a linked WhatsApp account, whatever drives it."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

# WhatsApp Web's CallState enum (WAWebVoipWaCallEnums.CallState) -> our call states
WA_CALL_STATES = {
    0: "ended",          # None
    1: "calling",        # Calling: offer sent, waiting for the peer
    2: "ringing",        # PreacceptReceived: a device of the peer is ringing
    3: "incoming",       # ReceivedCall
    4: "connecting",     # AcceptSent
    5: "connecting",     # AcceptReceived
    6: "active",         # CallActive
    7: "elsewhere",      # CallActiveElseWhere: answered on another device
    8: "incoming",       # ReceivedCallWithoutOffer
    9: "other",          # Rejoining
    10: "other",         # Link
    11: "active",        # ConnectedLonely
    12: "calling",       # PreCalling
    13: "ended",         # CallStateEnding
    14: "calling",       # CallBCallStarting
}
# WAWebVoipWaCallEnums call log results
WA_LOG_RESULTS = {2: "missed", 3: "declined", 4: "cancelled", 5: "unavailable", 6: "answered elsewhere"}

TERMINAL = ("ended", "elsewhere")


class WaError(Exception):
    """A WhatsApp operation failed; `code` is machine readable (busy, not_on_whatsapp, ...)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class WaCall:
    id: str
    account_id: str
    state: str                       # incoming, calling, ringing, connecting, active, elsewhere, ended
    outgoing: bool
    peer: dict = field(default_factory=dict)    # jid, lid, pn_jid, number, name
    is_video: bool = False
    is_group: bool = False
    reason: str = ""                 # why it ended
    created: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def ended(self) -> bool:
        return self.state in TERMINAL

    def peer_label(self) -> str:
        p = self.peer
        if p.get("name"):
            return p["name"]
        if p.get("number"):
            return "+" + p["number"]
        return (p.get("jid") or "unknown").split("@")[0]

    def info(self) -> dict:
        return {"id": self.id, "state": self.state, "outgoing": self.outgoing, "peer": self.peer,
                "peer_label": self.peer_label(), "reason": self.reason, "is_video": self.is_video,
                "is_group": self.is_group, "created": self.created}


def end_reason(snap: dict, state: str) -> str:
    """Human-readable reason for a finished call snapshot from WhatsApp Web."""
    if state == "elsewhere":
        return "answered on another device"
    if snap.get("peerBusy"):
        return "busy"
    if snap.get("everConnected"):
        return "hung up"
    res = WA_LOG_RESULTS.get(snap.get("logResult") or 0)
    if res:
        return res
    if snap.get("failedReason"):
        return f"failed ({snap['failedReason']})"
    return "not answered" if snap.get("outgoing") else "ended"


class AudioPipe(ABC):
    """Call audio between us and WhatsApp: G.711 at 8 kHz in both directions.

    `on_audio(payload)` receives what the WhatsApp peer says; `write(payload)` sends audio to them.
    """

    codec: str

    @abstractmethod
    def write(self, payload: bytes) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    def info(self) -> dict:
        return {}


CallListener = Callable[[WaCall], None]


class WaRuntime(ABC):
    """One linked WhatsApp account."""

    def __init__(self, account_id: str, name: str):
        self.account_id = account_id
        self.name = name
        self.state = "stopped"       # stopped, starting, loading, qr, authenticated, ready, disconnected, failed
        self.detail = ""
        self.since = time.time()
        self.qr: str | None = None   # PNG data URL of the current QR code
        self.pairing_code: str | None = None
        self.me: dict | None = None
        self.calls: dict[str, WaCall] = {}
        self.on_call: CallListener | None = None
        self.diag: dict = {}

    def set_state(self, state: str, detail: str = "") -> None:
        if state != self.state:
            self.since = time.time()
        self.state = state
        self.detail = detail
        if state != "qr":
            self.qr = None
        if state == "ready":
            self.pairing_code = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def active_call(self) -> WaCall | None:
        return next((c for c in self.calls.values() if not c.ended), None)

    def _emit_call(self, call: WaCall) -> None:
        if call.ended:
            self.calls.pop(call.id, None)
        else:
            self.calls[call.id] = call
        if self.on_call:
            self.on_call(call)

    def status(self) -> dict:
        return {"id": self.account_id, "name": self.name, "state": self.state, "detail": self.detail,
                "since": self.since, "qr": self.qr, "pairing_code": self.pairing_code, "me": self.me,
                "calls": [c.info() for c in self.calls.values()], "diag": self.diag}

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def contacts(self) -> list[dict]: ...

    @abstractmethod
    async def lookup(self, number: str) -> dict | None:
        """{jid, number, name} if the number has WhatsApp, else None."""

    @abstractmethod
    async def dial(self, target: str) -> WaCall: ...

    @abstractmethod
    async def accept(self, call_id: str) -> None: ...

    @abstractmethod
    async def reject(self, call_id: str) -> None: ...

    @abstractmethod
    async def hangup(self, call_id: str) -> None: ...

    @abstractmethod
    async def logout(self) -> None: ...

    @abstractmethod
    async def request_pairing_code(self, phone: str) -> str: ...

    @abstractmethod
    async def open_audio(self, codec: str, on_audio: Callable[[bytes], None]) -> AudioPipe:
        """Start call audio (G.711 `codec`, 20 ms frames) for the current call."""

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def screenshot(self) -> bytes | None:
        return None

    async def diagnostics(self) -> dict:
        return dict(self.diag)
