"""The engine: configuration -> SIP registrations + WhatsApp accounts, and call routing."""

from __future__ import annotations

import array
import asyncio
import logging
import time
from collections import OrderedDict

from .media import g711, tts
from .media.rtp import PortAllocator
from .models import Bridge
from .sessions import InboundSession, OutboundSession, Session
from .settings import Settings
from .sip.ua import AccountConfig, Call, UserAgent
from .store import Store
from .wa.base import WaCall
from .wa.manager import WaManager

log = logging.getLogger("wa2sip.engine")

PROMPT_CACHE = 256


class Engine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.ports = PortAllocator(settings.rtp_port_min, settings.rtp_port_max)
        self.ua = UserAgent(settings.sip_port, self.ports, settings.advertise_ip,
                            user_agent=f"wa2sip/{settings.version}", bind=settings.sip_bind)
        self.ua.on_incoming = self.on_incoming
        self.wa = WaManager(settings, self.on_wa_call)
        self.tts = tts.Tts(data_dir=settings.data_dir, piper_port=settings.piper_port)
        self.sessions: dict[str, Session] = {}
        self.wa_owner: dict[str, Session] = {}            # WhatsApp call id -> session
        self.recent_wa: OrderedDict[str, WaCall] = OrderedDict()
        self._audio: OrderedDict[tuple, bytes] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self.started = time.time()

    @property
    def ringback_style(self) -> str:
        return self.store.config.settings.ringback_style

    # -- lifecycle ----------------------------------------------------------------------------
    async def start(self) -> None:
        await self.ua.start()
        await self.wa.start()
        await self.apply_config()

    async def stop(self) -> None:
        for s in list(self.sessions.values()):
            if s.task:
                s.task.cancel()
        await asyncio.gather(*(s.task for s in self.sessions.values() if s.task), return_exceptions=True)
        await self.ua.stop()
        await self.wa.stop()
        await self.tts.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def apply_config(self) -> None:
        cfg = self.store.config
        accounts = []
        for ext in cfg.extensions:
            pbx = self.store.pbx(ext.pbx_id)
            if not ext.enabled or not pbx or not pbx.enabled:
                continue
            accounts.append(AccountConfig(
                id=ext.id, server=pbx.host, port=pbx.port, username=ext.username, password=ext.password,
                auth_username=ext.auth_username, domain=pbx.domain, display_name=ext.display_name,
                expires=pbx.expires, contact_user=ext.contact_user))
        await self.ua.set_accounts(accounts)
        await self.wa.set_accounts(cfg.wa_accounts)
        self._audio.clear()
        self._spawn(self._prewarm())

    # -- prompts ------------------------------------------------------------------------------------
    def voice_of(self, bridge: Bridge) -> tuple[str, int]:
        s = self.store.config.settings
        return bridge.voice or s.default_voice, bridge.speed or s.default_speed

    def used_voices(self) -> set[str]:
        s = self.store.config.settings
        return {b.voice or s.default_voice for b in self.store.config.bridges} | {s.default_voice}

    async def prompt_pcm(self, text: str, bridge: Bridge) -> array.array:
        voice, speed = self.voice_of(bridge)
        return await self.tts.pcm(text, voice, speed)

    async def prompt_audio(self, text: str, bridge: Bridge, codec: str) -> bytes:
        voice, speed = self.voice_of(bridge)
        key = (" ".join(text.split()), voice, speed, codec)
        hit = self._audio.get(key)
        if hit is not None:
            self._audio.move_to_end(key)
            return hit
        data = g711.encode_pcm(await self.tts.pcm(text, voice, speed), codec)
        self._audio[key] = data
        while len(self._audio) > PROMPT_CACHE:
            self._audio.popitem(last=False)
        return data

    @staticmethod
    def menu_text(bridge: Bridge) -> str:
        options = [(code, c.label()) for code, c in bridge.menu_codes()]
        text = tts.menu_text(bridge.ivr_greeting, bridge.ivr_option_text, options)
        if bridge.dial_number:
            text += " " + tts.fill(bridge.ivr_dial_text, digit=bridge.dial_digit)
        return text.strip()

    async def menu_audio(self, bridge: Bridge, codec: str) -> bytes:
        return await self.prompt_audio(self.menu_text(bridge), bridge, codec)

    def bridge_prompts(self, b: Bridge) -> list[str]:
        texts = [self.menu_text(b), b.ivr_invalid_text, b.ivr_goodbye_text, b.ivr_offline_text,
                 b.ivr_wa_busy_text, b.ivr_not_on_wa_text]
        if b.dial_number:
            texts.append(b.ivr_enter_text)
        for c in b.contacts:
            name = c.label()
            for tpl in (b.ivr_calling_text, b.ivr_failed_text, b.ivr_busy_text):
                texts.append(tts.fill(tpl, name=name, number=c.number))
            if b.announce:
                texts.append(tts.fill(b.announce_text, name=name, number=c.number))
        return [t for t in texts if t.strip()]

    async def _prewarm(self) -> None:
        """Download missing natural voices and synthesize the prompts calls will need."""
        piper = self.tts.piper
        if piper and piper.available:
            for voice in self.used_voices():
                key = tts.piper_key(voice)
                if key and not piper.is_installed(key):
                    try:
                        await piper.download(key)
                    except Exception as e:
                        log.warning("could not download voice %s: %s", key, e)
        try:
            for b in self.store.config.bridges:
                if b.enabled:
                    for text in self.bridge_prompts(b):
                        await self.prompt_pcm(text, b)
        except Exception as e:
            log.debug("prompt prewarm failed: %s", e)

    # -- PBX -> WhatsApp --------------------------------------------------------------------------------
    async def on_incoming(self, call: Call) -> None:
        ext_id = call.account.cfg.id
        bridge = self.store.bridge_for_extension(ext_id)
        who = call.remote_user or "unknown"
        if not bridge or not bridge.outbound_enabled:
            log.info("call from %s to %s rejected: no bridge takes calls on this extension",
                     who, call.account.cfg.username)
            call.reject(480, "Temporarily Unavailable")
            return
        if bridge.allowed_callers and who not in bridge.allowed_callers:
            log.info("call from %s to %s rejected: caller not allowed on bridge '%s'",
                     who, call.account.cfg.username, bridge.name or bridge.id)
            call.reject(403, "Forbidden")
            return
        runtime = self.wa.get(bridge.wa_account_id)
        session = OutboundSession(self, bridge, call, runtime)
        self._start_session(session)
        log.info("call from %s on extension %s -> bridge '%s'", who, call.account.cfg.username,
                 bridge.name or bridge.id)

    def _start_session(self, session: Session) -> None:
        self.sessions[session.id] = session

        async def run():
            try:
                await session.run()  # type: ignore[attr-defined]
            finally:
                self.sessions.pop(session.id, None)
        session.task = self._spawn(run())

    # -- WhatsApp -> PBX -----------------------------------------------------------------------------------
    def on_wa_call(self, call: WaCall) -> None:
        """Every WhatsApp call state change of every account lands here."""
        if call.ended:
            self.recent_wa[call.id] = call
            while len(self.recent_wa) > 50:
                self.recent_wa.popitem(last=False)
        owner = self.wa_owner.get(call.id)
        if owner:
            owner.on_wa_event(call)
            return
        if call.outgoing or call.state != "incoming":
            return
        rt = self.wa.get(call.account_id)
        if not rt:
            return
        if call.is_group:
            log.info("ignoring WhatsApp group call from %s", call.peer_label())
            return
        if self.wa_busy(call.account_id):
            log.info("WhatsApp call from %s while another bridged call is running - not ringing",
                     call.peer_label())
            return
        bridge, contact, why = self.store.route_wa_call(call.account_id, call.peer)
        if not bridge:
            acc = self.store.wa_account(call.account_id)
            if acc and acc.unrouted == "reject":
                log.info("declining WhatsApp call from %s: %s", call.peer_label(), why)
                self._spawn(rt.reject(call.id))
            else:
                log.info("WhatsApp call from %s not bridged: %s", call.peer_label(), why)
            return
        targets = (contact.ring if contact and contact.ring else None) or bridge.ring_targets
        if not targets:
            log.warning("WhatsApp call from %s: bridge '%s' has no extensions to ring", call.peer_label(),
                        bridge.name or bridge.id)
            return
        session = InboundSession(self, bridge, contact, call, rt)
        self.wa_owner[call.id] = session
        self._start_session(session)

    def claim_wa_call(self, call: WaCall, session: Session) -> None:
        """A session placed `call`: route its events there (and replay what was missed)."""
        self.wa_owner[call.id] = session
        rt = self.wa.get(call.account_id)
        latest = (rt.calls.get(call.id) if rt else None) or self.recent_wa.get(call.id)
        if latest and latest.state != call.state:
            session.on_wa_event(latest)

    def release_wa_call(self, call_id: str, session: Session) -> None:
        if self.wa_owner.get(call_id) is session:
            del self.wa_owner[call_id]

    def wa_busy(self, account_id: str, exclude: Session | None = None) -> bool:
        return any(s is not exclude and s.bridge.wa_account_id == account_id and s.wa_call
                   and not s.wa_call.ended for s in self.sessions.values())

    # -- history / status ----------------------------------------------------------------------------------
    def record(self, s: Session, direction: str, pbx_party: str) -> None:
        now = time.time()
        dur = int(now - s.connected_at) if s.connected_at else 0
        self.store.add_history({
            "id": s.id, "direction": direction, "bridge": s.bridge.name or s.bridge.id,
            "wa_account": s.bridge.wa_account_id, "wa_peer": s.peer_name, "wa_number": s.peer_number,
            "pbx_party": pbx_party, "visited": s.visited, "started_at": s.started,
            "connected_at": s.connected_at, "ended_at": now, "duration": dur,
            "result": s.result or "ended",
        })

    async def hangup(self, session_id: str) -> bool:
        s = self.sessions.get(session_id)
        if not s:
            return False
        if s.leg and s.leg.call.state != "ended":
            await s.leg.call.hangup("hung up from the web UI")
        if s.task:
            s.task.cancel()
        return True

    def status(self) -> dict:
        cfg = self.store.config
        exts = {}
        for ext in cfg.extensions:
            acc = self.ua.accounts.get(ext.id)
            exts[ext.id] = acc.status() if acc else {"state": "disabled"}
        return {
            "uptime": time.time() - self.started,
            "extensions": exts,
            "wa": self.wa.status(),
            "sessions": [s.info() for s in self.sessions.values()],
            "tts": {"piper": bool(self.tts.piper and self.tts.piper.available), "espeak": self.tts.available},
        }
