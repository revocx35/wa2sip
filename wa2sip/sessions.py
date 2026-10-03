"""Call sessions: one PBX call leg bridged to one WhatsApp call, plus the IVR menu.

OutboundSession   a PBX phone calls the bridge's extension -> IVR menu -> WhatsApp call
InboundSession    a WhatsApp call comes in -> ring PBX extensions -> first to answer is bridged

Audio while connected (G.711, 20 ms frames, no transcoding in Python):

    WhatsApp peer -> Chromium -> PulseAudio speaker sink -> parec -> Pacer -> RTP -> PBX
    PBX -> RTP -> pacat -> PulseAudio mic sink -> Chromium "microphone" -> WhatsApp peer

Prompts and tones for the PBX side win over WhatsApp audio on the same 20 ms clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from .media import g711, tones, tts
from .media.dtmf import DtmfDetector
from .media.rtp import Pacer, RtpPacket
from .models import Bridge, BridgeContact, digits_only
from .sip.ua import Call
from .wa.base import AudioPipe, WaCall, WaError, WaRuntime

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("wa2sip.session")

DTMF_EVENTS = "0123456789*#ABCD"
INTERDIGIT = 2.5            # seconds between keys of a multi-digit menu code
NUMBER_INTERDIGIT = 6.0     # dial-a-number: max pause between digits
PIN_FIRST_KEY = 10.0        # PIN: time for the first key after the prompt
PIN_INTERDIGIT = 5.0        # PIN: max pause between keys (then the PIN counts as entered)
PIN_MAX_DIGITS = 16
RTP_TIMEOUT = 60.0


class SipLeg:
    """Media side of one SIP call: paced audio out, prompts/tones with priority, keys in."""

    def __init__(self, call: Call, on_dtmf, on_audio):
        self.call = call
        self.on_dtmf = on_dtmf
        self.on_audio = on_audio          # (payload, codec) from the phone
        self.pacer: Pacer | None = None
        self._prompt = b""
        self._prompt_pos = 0
        self._prompt_done = asyncio.Event()
        self._prompt_done.set()
        self._tone = b""
        self._tone_pos = 0
        self._last_dtmf_ts: int | None = None
        self._inband: DtmfDetector | None = None

    @property
    def codec(self) -> str:
        return self.call.codec or "PCMA"

    def start(self) -> None:
        self.pacer = Pacer(self._emit, g711.SILENCE.get(self.codec, 0xD5))
        self.pacer.start()
        if self.call.rtp:
            self.call.rtp.on_packet = self._on_rtp
        self.call.on_dtmf = self.on_dtmf

    def stop(self) -> None:
        if self.pacer:
            self.pacer.stop()
            self.pacer = None
        if self.call.rtp:
            self.call.rtp.on_packet = None
        self.call.on_dtmf = None
        self._prompt_done.set()

    # -- out (to the phone) -------------------------------------------------------------
    def feed(self, payload: bytes, codec: str) -> None:
        """Far-end (WhatsApp) audio."""
        if self.pacer:
            self.pacer.push(payload if codec == self.codec else g711.convert(payload, codec, self.codec))

    def _emit(self, frame: bytes) -> None:
        silence = g711.SILENCE.get(self.codec, 0xD5)
        if self._prompt:
            chunk = self._prompt[self._prompt_pos:self._prompt_pos + 160]
            self._prompt_pos += 160
            if self._prompt_pos >= len(self._prompt):
                self.stop_prompt()
            frame = chunk + bytes([silence]) * (160 - len(chunk))
        elif self._tone:
            if self._tone_pos >= len(self._tone):
                self._tone_pos = 0
            chunk = self._tone[self._tone_pos:self._tone_pos + 160]
            self._tone_pos += 160
            frame = chunk + bytes([silence]) * (160 - len(chunk))
        call = self.call
        if call.state == "active" and call.rtp and call.negotiated:
            call.rtp.send(call.negotiated.remote_pt, frame, len(frame))

    def play(self, audio: bytes) -> None:
        self._prompt, self._prompt_pos = audio, 0
        if audio:
            self._prompt_done.clear()
        else:
            self._prompt_done.set()

    def stop_prompt(self) -> None:
        self._prompt = b""
        self._prompt_done.set()

    async def wait_prompt(self) -> None:
        await self._prompt_done.wait()

    @property
    def prompting(self) -> bool:
        return bool(self._prompt)

    def tone(self, audio: bytes | None) -> None:
        self._tone = audio or b""
        self._tone_pos = 0

    def flush(self) -> None:
        """Drop buffered far-end audio (e.g. after a prompt, so it doesn't play late)."""
        if self.pacer:
            self.pacer.buf.clear()
            self.pacer.primed = False

    # -- in (from the phone) --------------------------------------------------------------
    def _on_rtp(self, pkt: RtpPacket) -> None:
        call = self.call
        neg = call.negotiated
        if not neg or call.state != "active":
            return
        if neg.dtmf_pt is not None and pkt.pt == neg.dtmf_pt:
            if len(pkt.payload) >= 4 and pkt.ts != self._last_dtmf_ts:
                self._last_dtmf_ts = pkt.ts
                if pkt.payload[0] < len(DTMF_EVENTS):
                    self.on_dtmf(DTMF_EVENTS[pkt.payload[0]])
            return
        if pkt.pt != neg.remote_pt:
            return
        if neg.dtmf_pt is None:             # no RFC 4733: listen for key tones in the audio
            if self._inband is None:
                self._inband = DtmfDetector()
            table = g711.DECODE[self.codec]
            digit = self._inband.feed([table[x] for x in pkt.payload])
            if digit:
                self.on_dtmf(digit)
        self.on_audio(pkt.payload, self.codec)

    def info(self) -> dict:
        p = self.pacer
        return {"codec": self.call.codec, "underruns": p.underruns if p else 0,
                "buffer_ms": int(len(p.buf) / 8) if p else 0, "prompting": self.prompting}


class KeyBuffer:
    """DTMF keys of one call leg, for a PIN prompt: queued, and every key cuts the prompt short."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.leg: SipLeg | None = None

    def __call__(self, digit: str) -> None:
        self.queue.put_nowait(digit)
        if self.leg:
            self.leg.stop_prompt()


class Session:
    """Common parts: the WhatsApp call, its audio pipe, history, status."""

    kind = "session"

    def __init__(self, engine: "Engine", bridge: Bridge, runtime: WaRuntime | None):
        self.engine = engine
        self.bridge = bridge
        self.runtime = runtime
        self.id = ""
        self.leg: SipLeg | None = None
        self.wa_call: WaCall | None = None
        self.wa_events: asyncio.Queue[WaCall] = asyncio.Queue()
        self.pipe: AudioPipe | None = None
        self.phase = "starting"
        self.peer_name = ""
        self.peer_number = ""
        self.started = time.time()
        self.connected_at: float | None = None
        self.result = ""
        self.task: asyncio.Task | None = None
        self.visited: list[str] = []

    # -- WhatsApp side ---------------------------------------------------------------------
    def on_wa_event(self, call: WaCall) -> None:
        if self.wa_call and call.id == self.wa_call.id:
            self.wa_call = call
            self.wa_events.put_nowait(call)

    def _wa_audio(self, payload: bytes) -> None:
        if self.leg and self.phase == "connected" and self.pipe:
            self.leg.feed(payload, self.pipe.codec)

    def _phone_audio(self, payload: bytes, codec: str) -> None:
        pipe = self.pipe
        if pipe and self.phase == "connected":
            pipe.write(payload if codec == pipe.codec else g711.convert(payload, codec, pipe.codec))

    async def _open_pipe(self) -> None:
        if self.pipe or not self.runtime or not self.leg:
            return
        try:
            self.pipe = await self.runtime.open_audio(self.leg.codec, self._wa_audio)
        except Exception as e:
            log.error("call audio could not be opened: %s", e)

    async def _close_pipe(self) -> None:
        pipe, self.pipe = self.pipe, None
        if pipe:
            await pipe.close()

    async def _end_wa(self, why: str) -> None:
        call = self.wa_call
        rt = self.runtime
        if not call or call.ended or not rt:
            return
        try:
            if call.state == "incoming" and not getattr(self, "accepted", False):
                await rt.reject(call.id)
            else:
                await rt.hangup(call.id)
        except WaError as e:
            log.warning("could not end WhatsApp call (%s): %s", why, e)

    # -- prompts -----------------------------------------------------------------------------
    def fill(self, template: str, **extra: str) -> str:
        values = {"name": self.peer_name or "", "number": self.peer_number or "",
                  "bridge": self.bridge.name or ""}
        values.update(extra)
        return tts.fill(template, **values)

    async def say(self, text: str, wait: bool = True, leg: SipLeg | None = None) -> None:
        leg = leg or self.leg
        if not leg or not text.strip() or leg.call.state != "active":
            return
        audio = await self.engine.prompt_audio(text, self.bridge, leg.codec)
        leg.play(audio)
        if wait:
            done = asyncio.create_task(leg.wait_prompt())
            gone = asyncio.create_task(leg.call.ended.wait())
            try:
                await asyncio.wait({done, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                done.cancel()
                gone.cancel()

    # -- PIN ----------------------------------------------------------------------------------------
    async def ask_pin(self, leg: SipLeg, digits: asyncio.Queue, who: str) -> bool:
        """Ask for the bridge PIN on `leg` (keys arrive in `digits`). Never logs the digits."""
        b = self.bridge
        for attempt in range(1, b.pin_attempts + 1):
            if leg.call.state != "active":
                return False
            if attempt > 1:                       # keys typed ahead count only for the first try
                while not digits.empty():
                    digits.get_nowait()
            if digits.empty():
                await self.say(b.pin_prompt_text, wait=False, leg=leg)
            entered = await self._collect_pin(leg, digits)
            if entered and self.engine.pins.check(b.id, entered, b.pin, who):
                log.info("[%s] PIN accepted from %s", b.name or b.id, who)
                return True
            if leg.call.state != "active":
                return False
            log.warning("[%s] %s from %s (attempt %d of %d)", b.name or b.id,
                        "wrong PIN" if entered else "no PIN entered", who, attempt, b.pin_attempts)
            await self.say(b.pin_wrong_text, leg=leg)
        return False

    @staticmethod
    async def _collect_pin(leg: SipLeg, digits: asyncio.Queue) -> str:
        """Keys until '#' (or a pause); '*' starts over. The prompt's length counts as extra time."""
        entered = ""
        timeout = PIN_FIRST_KEY + 8       # the prompt itself takes a few seconds
        while len(entered) < PIN_MAX_DIGITS:
            try:
                d = await asyncio.wait_for(digits.get(), timeout)
            except TimeoutError:
                break
            leg.stop_prompt()
            if d == "#":
                break
            if d == "*":
                entered = ""
            elif d.isdigit():
                entered += d
            timeout = PIN_INTERDIGIT
        return entered

    # -- status --------------------------------------------------------------------------------
    def info(self) -> dict:
        call = self.leg.call if self.leg else None
        return {
            "id": self.id, "kind": self.kind, "phase": self.phase,
            "bridge": {"id": self.bridge.id, "name": self.bridge.name},
            "wa_account": self.bridge.wa_account_id,
            "peer": {"name": self.peer_name, "number": self.peer_number},
            "wa_call": self.wa_call.info() if self.wa_call else None,
            "sip": call.info() if call else None,
            "leg": self.leg.info() if self.leg else None,
            "audio": self.pipe.info() if self.pipe else None,
            "started": self.started, "connected_at": self.connected_at,
        }


# =====================================================================================================
class OutboundSession(Session):
    """PBX -> WhatsApp: answer the extension, run the menu, call the chosen contact."""

    kind = "pbx-to-wa"

    def __init__(self, engine: "Engine", bridge: Bridge, call: Call, runtime: WaRuntime | None):
        super().__init__(engine, bridge, runtime)
        self.call = call
        self.id = call.id
        self.caller = call.remote_display or call.remote_user or "unknown"
        self.digits: asyncio.Queue[str] = asyncio.Queue()

    async def run(self) -> None:
        call = self.call
        self.leg = SipLeg(call, self._on_dtmf, self._phone_audio)
        try:
            await call.answer()
            self.leg.start()
            main = asyncio.create_task(self._main_after_settle())
            hung_up = asyncio.create_task(call.ended.wait())
            watchdog = asyncio.create_task(self._watchdog())
            try:
                await asyncio.wait({main, hung_up}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (main, hung_up, watchdog):
                    t.cancel()
                await asyncio.gather(main, return_exceptions=True)
            if main.done() and not main.cancelled() and main.exception():
                raise main.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("bridge session failed")
        finally:
            await self._finish()

    async def _main_after_settle(self) -> None:
        await asyncio.sleep(0.4)                  # let the media path settle before speaking
        await self._main()

    async def _main(self) -> None:
        b = self.bridge
        rt = self.runtime
        if b.pin_required("outbound"):
            self.phase = "pin"
            if not await self.ask_pin(self.leg, self.digits, self.caller):
                self.result = "wrong PIN"
                await self._goodbye()
                return
        if not rt or not rt.ready:
            log.info("[%s] WhatsApp is not connected - telling the caller", b.name or b.id)
            self.result = "WhatsApp not connected"
            await self.say(b.ivr_offline_text)
            await self._goodbye()
            return
        options = b.menu_codes()
        if len(options) == 1 and not b.dial_number and not b.menu_always:
            contact = options[0][1]
            outcome = await self._call_contact(contact.target(), contact.label())
            if outcome == "failed" and self.call.state == "active":
                await self._goodbye()
            elif outcome == "menu" and self.call.state == "active":
                await self._menu(options)
            elif outcome == "ended" and self.call.state == "active":
                await self.call.hangup("WhatsApp call ended")
            return
        await self._menu(options)

    async def _goodbye(self) -> None:
        await self.say(self.bridge.ivr_goodbye_text)
        await self.call.hangup("goodbye")

    # -- menu ------------------------------------------------------------------------------------------
    async def _menu(self, options: list[tuple[str, BridgeContact]]) -> None:
        b = self.bridge
        codes = {code: c for code, c in options}
        valid = set(codes)
        if b.dial_number:
            valid.add(b.dial_digit)
        if not valid:
            log.warning("[%s] bridge has no contacts in its menu", b.name or b.id)
            await self.say(b.ivr_offline_text)
            await self._goodbye()
            return
        misses = 0
        while self.call.state == "active":
            if misses >= b.ivr_repeats:
                self.result = self.result or "no selection"
                await self._goodbye()
                return
            self.phase = "menu"
            menu = await self.engine.menu_audio(b, self.leg.codec)
            if self.digits.empty():              # a key pressed during the last prompt skips the menu
                self.leg.play(menu)
            code = await self._collect_code(valid, len(menu) / 8000 + b.ivr_timeout)
            if self.call.state != "active":
                return
            if code is None:
                misses += 1
                continue
            self.leg.stop_prompt()
            if b.dial_number and code == b.dial_digit:
                number = await self._collect_number()
                if number is None:
                    misses += 1
                    continue
                target, label, spoken = number, f"+{number}", " ".join(number)   # digits read one by one
            elif code in codes:
                contact = codes[code]
                target, label = contact.target(), contact.label()
                spoken = label
            else:
                log.info("IVR: invalid choice %s", code)
                await self.say(b.ivr_invalid_text)
                misses += 1
                continue
            misses = 0
            log.info("IVR: %s chose %s -> %s", self.caller, code, label)
            outcome = await self._call_contact(target, label, spoken)
            if self.call.state != "active":
                return
            if outcome == "ended":
                if b.after_call == "menu":
                    continue
                await self.call.hangup("WhatsApp call ended")
                return
            # "failed" (announced already) or "menu" (caller pressed the menu key): menu again

    async def _wait_digit(self, timeout: float) -> str | None:
        try:
            return await asyncio.wait_for(self.digits.get(), timeout)
        except TimeoutError:
            return None

    async def _collect_code(self, valid: set[str], first_timeout: float) -> str | None:
        """One menu code; multi-digit codes end as soon as they are unambiguous."""
        d = await self._wait_digit(first_timeout)
        if d is None:
            return None
        buf = d
        while True:
            longer = any(c != buf and c.startswith(buf) for c in valid)
            if not longer:
                return buf
            d = await self._wait_digit(INTERDIGIT)
            if d is None:
                return buf
            buf += d

    async def _collect_number(self) -> str | None:
        b = self.bridge
        self.phase = "dial-number"
        while not self.digits.empty():
            self.digits.get_nowait()
        await self.say(b.ivr_enter_text, wait=False)
        number = ""
        deadline = NUMBER_INTERDIGIT + 20         # time for the prompt + the first digit
        while True:
            d = await self._wait_digit(deadline)
            if d is None:
                break
            self.leg.stop_prompt()
            if d == "#":
                break
            if d == "*":                          # start over
                number = ""
                continue
            number += d
            deadline = NUMBER_INTERDIGIT
        number = normalize_number(number, b.national_prefix, b.country_code)
        if len(number) < 6:
            if number:
                await self.say(b.ivr_invalid_text)
            return None
        return number

    # -- the WhatsApp call ---------------------------------------------------------------------------------
    async def _call_contact(self, target: str, label: str, spoken: str = "") -> str:
        """Call `target` on WhatsApp and bridge it. Returns 'ended', 'failed' or 'menu'.

        `label` is shown (history, status); `spoken` is what the prompts say (default: label).
        """
        b = self.bridge
        rt = self.runtime
        spoken = spoken or label
        self.peer_name = label
        self.peer_number = digits_only(target) if "@" not in target else ""
        self.visited.append(label)
        if not rt or not rt.ready:
            await self.say(b.ivr_offline_text)
            return "failed"
        if rt.active_call() or self.engine.wa_busy(rt.account_id, self):
            await self.say(b.ivr_wa_busy_text)
            return "failed"
        self.phase = "dialing"
        self.wa_events = asyncio.Queue()
        announce = asyncio.create_task(self.say(self.fill(b.ivr_calling_text, name=spoken)))
        try:
            wa_call = await rt.dial(target)
        except WaError as e:
            log.info("WhatsApp call to %s failed: %s", label, e)
            self.result = f"WhatsApp: {e}"
            announce.cancel()                     # cut "Calling ..." short and say what happened
            self.leg.stop_prompt()
            if e.code == "not_on_whatsapp":
                await self.say(b.ivr_not_on_wa_text)
            elif e.code == "busy":
                await self.say(b.ivr_wa_busy_text)
            else:
                await self.say(self.fill(b.ivr_failed_text, name=spoken))
            return "failed"
        self.wa_call = wa_call
        self.engine.claim_wa_call(wa_call, self)
        if wa_call.peer.get("number"):
            self.peer_number = wa_call.peer["number"]
        await self._open_pipe()
        await announce
        if b.ringback and not (self.wa_call and self.wa_call.state == "active"):
            self.leg.tone(tones.ringback(self.leg.codec, self.engine.ringback_style))
        outcome = await self._dialing_and_talking(label, spoken)
        self.leg.tone(None)
        await self._close_pipe()
        self.engine.release_wa_call(wa_call.id, self)
        return outcome

    async def _dialing_and_talking(self, label: str, spoken: str) -> str:
        b = self.bridge
        deadline = time.monotonic() + b.dial_timeout
        state = self.wa_call.state if self.wa_call else "ended"
        while True:
            if self.call.state != "active":
                await self._end_wa("PBX caller hung up")
                return "ended"
            if state == "active" and self.phase != "connected":
                self.phase = "connected"
                self.connected_at = self.connected_at or time.time()
                self.result = "connected"
                self.leg.tone(None)
                self.leg.stop_prompt()
                self.leg.flush()
                log.info("bridged PBX caller %s <-> WhatsApp %s", self.caller, label)
            if state in ("ended", "elsewhere"):
                reason = self.wa_call.reason if self.wa_call else ""
                if self.phase == "connected":
                    log.info("WhatsApp call with %s ended (%s)", label, reason or "hung up")
                    self.phase = "ended"
                    return "ended"
                log.info("WhatsApp call to %s failed: %s", label, reason or "ended")
                self.result = f"WhatsApp: {reason or 'not answered'}"
                self.leg.tone(None)
                text = b.ivr_busy_text if reason == "busy" else b.ivr_failed_text
                await self.say(self.fill(text, name=spoken))
                return "failed"
            if self.phase != "connected" and time.monotonic() > deadline:
                log.info("WhatsApp call to %s: no answer after %d s", label, b.dial_timeout)
                await self._end_wa("no answer")
                self.result = "WhatsApp: not answered"
                self.leg.tone(None)
                await self.say(self.fill(b.ivr_failed_text, name=spoken))
                return "failed"
            if self._menu_requested():          # while ringing (cancel) or talking
                log.info("caller pressed %s: back to the menu", b.menu_digit)
                self.leg.tone(None)
                await self._end_wa("back to menu")
                return "menu"
            try:
                ev = await asyncio.wait_for(self.wa_events.get(), 0.25)
                state = ev.state
            except TimeoutError:
                if self.wa_call:
                    state = self.wa_call.state

    def _menu_requested(self) -> bool:
        b = self.bridge
        found = False
        while not self.digits.empty():
            d = self.digits.get_nowait()
            if b.menu_digit and d == b.menu_digit and (len(b.menu_codes()) > 1 or b.dial_number or b.menu_always):
                found = True
        return found

    # -- keys, watchdog, teardown ----------------------------------------------------------------------
    def _on_dtmf(self, digit: str) -> None:
        log.debug("DTMF %s (%s)", "•" if self.phase == "pin" else digit, self.phase)
        self.digits.put_nowait(digit)
        if self.phase in ("menu", "dial-number", "pin") and self.leg:
            self.leg.stop_prompt()                # barge-in: a key press cuts any prompt short

    async def _watchdog(self) -> None:
        call = self.call
        while call.state == "active":
            await asyncio.sleep(1)
            if call.answered_at and time.time() - call.answered_at > self.bridge.max_call_seconds:
                await call.hangup("max call duration reached")
                return
            rtp = call.rtp
            last = rtp.last_rx if rtp and rtp.last_rx else None
            if call.answered_at and time.time() - call.answered_at > RTP_TIMEOUT and (
                    last is None or time.monotonic() - last > RTP_TIMEOUT):
                await call.hangup("no RTP from phone")
                return

    async def _finish(self) -> None:
        await self._end_wa("session ended")
        await self._close_pipe()
        if self.wa_call:
            self.engine.release_wa_call(self.wa_call.id, self)
        if self.leg:
            self.leg.stop()
        if self.call.state != "ended":
            await self.call.hangup("session ended")
        self.phase = "ended"
        self.engine.record(self, direction="pbx-to-wa", pbx_party=self.caller)


# =====================================================================================================
class InboundSession(Session):
    """WhatsApp -> PBX: ring the bridge's extensions; the first one to answer gets the call."""

    kind = "wa-to-pbx"

    def __init__(self, engine: "Engine", bridge: Bridge, contact: BridgeContact | None,
                 wa_call: WaCall, runtime: WaRuntime):
        super().__init__(engine, bridge, runtime)
        self.contact = contact
        self.wa_call = wa_call
        self.id = "wa-" + wa_call.id[:10]
        self.peer_name = (contact.label() if contact and contact.name else "") or wa_call.peer.get("name") or ""
        self.peer_number = wa_call.peer.get("number") or (contact.number if contact else "")
        if not self.peer_name:
            self.peer_name = f"+{self.peer_number}" if self.peer_number else wa_call.peer_label()
        self.targets = list((contact.ring if contact and contact.ring else None) or bridge.ring_targets)
        self.forks: list[Call] = []
        self.call: Call | None = None
        self.answered_by = ""
        self.accepted = False
        self.pin_failed = False

    async def run(self) -> None:
        try:
            await self._main()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("inbound WhatsApp session failed")
        finally:
            await self._finish()

    async def _main(self) -> None:
        b = self.bridge
        account = self.engine.ua.accounts.get(b.extension_id)
        if not account or account.state != "registered":
            log.warning("WhatsApp call from %s: extension of bridge '%s' is not registered - not ringing",
                        self.peer_name, b.name or b.id)
            self.result = "extension not registered"
            return
        display = self.fill(b.caller_name) or self.peer_name
        asserted = account.cfg.username
        if b.caller_number == "whatsapp" and self.peer_number:
            asserted = "+" + self.peer_number
        log.info("WhatsApp call from %s -> ringing %s via bridge '%s'", self.peer_name,
                 ", ".join(self.targets), b.name or b.id)
        self.phase = "ringing"
        for t in self.targets:
            try:
                self.forks.append(await account.dial(t, display_name=display, asserted_user=asserted))
            except Exception as e:
                log.warning("could not ring %s: %s", t, e)
        if not self.forks:
            self.result = "could not ring any extension"
            return
        won = await self._wait_answer()
        winner = won[0] if won else None
        for f in self.forks:
            if f is not winner and f.state != "ended":
                asyncio.create_task(f.hangup("answered elsewhere" if winner else "WhatsApp caller gone"))
        if not won:
            if self.wa_call and not self.wa_call.ended:
                self.result = "wrong PIN" if self.pin_failed else "no answer"
                if b.reject_unanswered:
                    log.info("nobody %s the WhatsApp call from %s - declining it",
                             "unlocked" if self.pin_failed else "answered", self.peer_name)
                    await self._end_wa("no answer")
            else:
                self.result = f"WhatsApp caller hung up ({self.wa_call.reason if self.wa_call else ''})"
                log.info("WhatsApp call from %s ended while ringing", self.peer_name)
            return
        await self._connected(*won)

    async def _wait_answer(self) -> tuple[Call, SipLeg, bool] | None:
        """The fork that answered (and entered the PIN, when one is required).

        Returns (call, its SipLeg, announced). With a PIN, every extension that picks up is asked
        for it while the others keep ringing; the first right PIN wins, wrong ones are hung up.
        """
        b = self.bridge
        need_pin = b.pin_required("inbound")
        deadline = time.monotonic() + b.ring_timeout
        checks: dict[Call, tuple[SipLeg, asyncio.Task | None]] = {}
        winner: Call | None = None
        try:
            while True:
                for f in self.forks:
                    if f.state != "active" or f in checks:
                        continue
                    if not need_pin:
                        leg = SipLeg(f, self._on_dtmf, self._phone_audio)
                        leg.start()
                        winner = f
                        return f, leg, False
                    keys = KeyBuffer()
                    leg = SipLeg(f, keys, lambda payload, codec: None)   # no audio to WhatsApp yet
                    keys.leg = leg
                    leg.start()
                    self.phase = "pin"
                    checks[f] = (leg, asyncio.create_task(self._verify_answerer(f, leg, keys.queue)))
                for f, (leg, task) in checks.items():
                    if task and task.done() and not task.cancelled() and task.result():
                        winner = f
                        leg.on_dtmf = self._on_dtmf
                        f.on_dtmf = self._on_dtmf
                        leg.on_audio = self._phone_audio
                        return f, leg, b.announce
                pending = any(t and not t.done() for _, t in checks.values())
                if not self.wa_call or self.wa_call.ended:
                    return None
                if not pending and all(f.state == "ended" for f in self.forks):
                    return None
                if time.monotonic() > deadline:
                    for f in self.forks:
                        if f.state == "ringing":
                            asyncio.create_task(f.hangup("no answer"))
                    if not pending:
                        return None
                try:
                    self.wa_call = await asyncio.wait_for(self.wa_events.get(), 0.1)
                except TimeoutError:
                    pass
        finally:
            for f, (leg, task) in checks.items():
                if f is not winner:
                    if task:
                        task.cancel()
                    leg.stop()

    async def _verify_answerer(self, call: Call, leg: SipLeg, digits: asyncio.Queue) -> bool:
        who = call.remote_user or "?"
        log.info("extension %s answered the WhatsApp call from %s - asking for the PIN", who, self.peer_name)
        await asyncio.sleep(0.3)                      # let the media path settle
        if self.bridge.announce:
            await self.say(self.fill(self.bridge.announce_text), leg=leg)
        if await self.ask_pin(leg, digits, who):
            return True
        self.pin_failed = True
        await self.say(self.bridge.ivr_goodbye_text, leg=leg)
        await call.hangup("wrong PIN")
        return False

    async def _connected(self, call: Call, leg: SipLeg, announced: bool) -> None:
        b = self.bridge
        rt = self.runtime
        self.call = call
        self.answered_by = call.remote_user
        self.leg = leg
        log.info("extension %s answered the WhatsApp call from %s", call.remote_user, self.peer_name)
        self.phase = "accepting"
        try:
            self.accepted = True
            await rt.accept(self.wa_call.id)
        except WaError as e:
            log.warning("could not accept the WhatsApp call: %s", e)
            self.result = f"accept failed: {e}"
            await self.say(self.fill(b.ivr_failed_text))
            return
        await self._open_pipe()
        if b.announce and not announced:
            await self.say(self.fill(b.announce_text))
        state = self.wa_call.state
        while True:
            if call.state != "active":
                await self._end_wa("PBX hung up")
                return
            if state == "active" and self.phase != "connected":
                self.phase = "connected"
                self.connected_at = time.time()
                self.result = "connected"
                self.leg.flush()
                log.info("bridged WhatsApp %s <-> extension %s", self.peer_name, call.remote_user)
            if state in ("ended", "elsewhere"):
                log.info("WhatsApp call with %s ended (%s)", self.peer_name, self.wa_call.reason)
                if self.phase != "connected":
                    self.result = f"WhatsApp: {self.wa_call.reason}"
                return
            if self.phase != "connected" and time.time() - (call.answered_at or time.time()) > 30:
                log.warning("WhatsApp call from %s did not connect after accepting", self.peer_name)
                self.result = "WhatsApp did not connect"
                await self._end_wa("did not connect")
                return
            if call.answered_at and time.time() - call.answered_at > b.max_call_seconds:
                return
            try:
                ev = await asyncio.wait_for(self.wa_events.get(), 0.25)
                state = ev.state
            except TimeoutError:
                state = self.wa_call.state

    def _on_dtmf(self, digit: str) -> None:
        log.debug("DTMF %s during WhatsApp call", digit)

    async def _finish(self) -> None:
        # a call we accepted is hung up with the PBX side; one that is still ringing is left
        # alone (it keeps ringing on the phone) unless we declined it on purpose above
        if self.wa_call and (self.accepted or self.wa_call.state != "incoming"):
            await self._end_wa("session ended")
        await self._close_pipe()
        self.engine.release_wa_call(self.wa_call.id, self)
        if self.leg:
            self.leg.stop()
        for f in self.forks:
            if f.state != "ended":
                with contextlib.suppress(Exception):
                    await f.hangup("WhatsApp call ended")
        self.phase = "ended"
        self.engine.record(self, direction="wa-to-pbx",
                           pbx_party=self.answered_by or ", ".join(self.targets))

    def info(self) -> dict:
        d = super().info()
        d["targets"] = self.targets
        d["ringing"] = [f.remote_user for f in self.forks if f.state == "ringing"]
        return d


def normalize_number(number: str, national_prefix: str, country_code: str) -> str:
    """Dial-a-number input -> international digits (no +)."""
    n = digits_only(number)
    if n.startswith("00"):
        return n[2:]
    if country_code and national_prefix and n.startswith(national_prefix):
        return country_code + n[len(national_prefix):]
    return n
