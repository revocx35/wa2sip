"""Engine + sessions with simulated WhatsApp accounts and in-process SIP phones (no PBX).

The phone UA doubles as the "PBX": it accepts REGISTER and the engine's calls to extension
2001, and calls the engine's extension directly by its contact user.
"""

import asyncio
import math

import pytest

from wa2sip.engine import Engine
from wa2sip.media import g711
from wa2sip.media.rtp import PortAllocator
from wa2sip.models import Bridge, BridgeContact, Config, Extension, Pbx, WaAccount
from wa2sip.settings import Settings
from wa2sip.sip.ua import Account, AccountConfig, UserAgent
from wa2sip.store import Store
from wa2sip.wa.fake import CONTACTS

from .conftest import free_port


def tone_power(payload: bytes, codec: str, freq: float) -> float:
    dec = g711.DECODE[codec]
    xs = [dec[b] for b in payload]
    if not xs:
        return -120.0
    k = 2 * math.cos(2 * math.pi * freq / 8000)
    s1 = s2 = 0.0
    for x in xs:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - k * s1 * s2
    return 10 * math.log10(max(p, 1e-9) / len(xs) ** 2 / 32768 ** 2 * 4)


class Rig:
    async def start(self, tmp_path, bridge_kw: dict, answer_delay: float = 0.3):
        self.phone_port = free_port()
        self.settings = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(),
                                 advertise_ip="127.0.0.1", rtp_port_min=32000, rtp_port_max=32039,
                                 wa_driver="fake")
        store = Store(tmp_path)
        pbx = Pbx(id="p", host="127.0.0.1", port=self.phone_port)
        ext = Extension(id="e", pbx_id="p", username="1009", password="x", contact_user="wa-test")
        bridge_kw.setdefault("contacts", [BridgeContact(wa_id=c["id"], number=c["number"], name=c["name"])
                                          for c in CONTACTS[:2]])
        bridge = Bridge(id="b", name="test", wa_account_id="w", extension_id="e", ring_targets=["2001"],
                        **bridge_kw)
        store.config = Config(pbxs=[pbx], extensions=[ext], wa_accounts=[WaAccount(id="w")], bridges=[bridge])
        store.config.settings.default_voice = "en-us"
        store.config.settings.default_speed = 250
        self.store = store
        # the phone + registrar
        self.ua = UserAgent(self.phone_port, PortAllocator(32100, 32139), advertise_ip="127.0.0.1")
        await self.ua.start()
        handler = self.ua.stack.request_handler

        async def handle(req, addr):
            if req.method == "REGISTER":
                self.ua.stack.respond(req, 200, "OK", [("Contact", f"{req.get('Contact')};expires=300")])
                return
            await handler(req, addr)
        self.ua.stack.request_handler = handle
        self.phone = Account(self.ua, AccountConfig(id="2001", server="127.0.0.1", port=self.settings.sip_port,
                                                    username="2001", password="", contact_user="2001"))
        self.ua.accounts["2001"] = self.phone
        self.incoming = []

        async def on_incoming(call):
            self.incoming.append(call)
            call.ring()
        self.ua.on_incoming = on_incoming
        self.engine = Engine(self.settings, store)
        await self.engine.start()
        self.rt = self.engine.wa.get("w")
        self.rt.answer_delay = answer_delay
        for _ in range(100):
            if self.engine.ua.accounts["e"].state == "registered":
                break
            await asyncio.sleep(0.02)
        assert self.engine.ua.accounts["e"].state == "registered"
        return self

    async def stop(self):
        await self.engine.stop()
        await self.ua.stop()

    async def call_bridge(self):
        call = await self.phone.dial(f"wa-test@127.0.0.1:{self.settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 5)
        rx = bytearray()

        def on_packet(pkt):
            if call.negotiated and pkt.pt == call.negotiated.remote_pt:
                rx.extend(pkt.payload)
        call.rtp.on_packet = on_packet
        return call, rx


async def send_audio(call, audio: bytes):
    loop = asyncio.get_running_loop()
    t = loop.time()
    for i in range(0, len(audio), 160):
        call.rtp.send(call.negotiated.remote_pt, audio[i:i + 160], 160)
        t += 0.02
        await asyncio.sleep(max(0.0, t - loop.time()))


async def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.mark.asyncio
async def test_pbx_to_whatsapp_menu_choice_and_audio(tmp_path):
    rig = await Rig().start(tmp_path, {"ivr_greeting": "Hi."})
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.8)
        assert len(rx) > 1000 and g711.level_dbfs(bytes(rx), call.codec) > -50     # the menu plays
        await call.send_dtmf("2")
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 8)
        session = next(iter(rig.engine.sessions.values()))
        assert session.peer_name == CONTACTS[1]["name"]
        mark = len(rx)
        await send_audio(call, g711.tone(call.codec, 700, 0.8, -10) + g711.silence(call.codec, 4000))
        assert tone_power(bytes(rx[mark:]), call.codec, 700) > -30           # echoed by the fake peer
        assert rig.rt.active_call() is not None
        await call.hangup()
        assert await wait_for(lambda: not rig.engine.sessions)
        assert rig.rt.active_call() is None                                   # WhatsApp side hung up
        assert rig.store.history[-1]["result"] == "connected"
        assert rig.store.history[-1]["wa_peer"] == CONTACTS[1]["name"]
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_single_contact_skips_menu_and_ringback_plays(tmp_path):
    only = [BridgeContact(number="491701112222", name="Never Answers")]     # ...2222: rings forever
    rig = await Rig().start(tmp_path, {"contacts": only, "dial_timeout": 10})
    try:
        call, rx = await rig.call_bridge()
        assert await wait_for(lambda: rig.rt.active_call() is not None, 5)  # dialled without a menu
        await asyncio.sleep(4.0)
        windows = [bytes(rx[i:i + 2000]) for i in range(0, len(rx) - 2000, 2000)]
        assert any(tone_power(w, call.codec, 425) > -30 and
                   tone_power(w, call.codec, 425) > tone_power(w, call.codec, 1000) + 20 for w in windows)
        await call.hangup()
        assert await wait_for(lambda: rig.rt.active_call() is None)
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_declined_whatsapp_call_returns_to_menu(tmp_path):
    contacts = [BridgeContact(number="491700000000", name="Decliner"), BridgeContact(number="4917011", name="X")]
    rig = await Rig().start(tmp_path, {"contacts": contacts}, answer_delay=0.2)
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.3)
        await call.send_dtmf("1")
        assert await wait_for(lambda: any(s.result.startswith("WhatsApp: declined")
                                          for s in rig.engine.sessions.values()), 8)
        assert await wait_for(lambda: any(s.phase == "menu" for s in rig.engine.sessions.values()), 15)
        await call.hangup()
        assert await wait_for(lambda: not rig.engine.sessions)
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_whatsapp_unavailable_is_announced(tmp_path):
    rig = await Rig().start(tmp_path, {})
    try:
        await rig.rt.stop()                               # account not connected
        call, rx = await rig.call_bridge()
        await asyncio.wait_for(call.ended.wait(), 15)     # offline text + goodbye, then hang-up
        assert g711.level_dbfs(bytes(rx), call.codec) > -50
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["result"] == "WhatsApp not connected"
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_whatsapp_to_pbx_rings_answers_and_bridges(tmp_path):
    rig = await Rig().start(tmp_path, {"announce_text": "WhatsApp call from {name}."})
    try:
        wa_call = rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        call = rig.incoming.pop()
        assert call.remote_display == f"WA {CONTACTS[0]['name']}"
        assert call.invite.get("P-Asserted-Identity").startswith(f'"WA {CONTACTS[0]["name"]}" <sip:+{CONTACTS[0]["number"]}@')
        await call.answer()
        rx = bytearray()
        call.rtp.on_packet = lambda pkt: rx.extend(pkt.payload)
        assert await wait_for(lambda: rig.rt.calls.get(wa_call.id) and rig.rt.calls[wa_call.id].state == "active")
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 10)
        mark = len(rx)
        await send_audio(call, g711.tone(call.codec, 900, 0.8, -10) + g711.silence(call.codec, 4000))
        assert tone_power(bytes(rx[mark:]), call.codec, 900) > -30
        await rig.rt.hangup(wa_call.id)                   # the WhatsApp side hangs up
        await asyncio.wait_for(call.ended.wait(), 5)
        assert await wait_for(lambda: not rig.engine.sessions)
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["direction"] == "wa-to-pbx"
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_unanswered_whatsapp_call_is_declined(tmp_path):
    rig = await Rig().start(tmp_path, {"ring_timeout": 5})
    try:
        wa_call = rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        ringing = rig.incoming.pop()
        await asyncio.wait_for(ringing.ended.wait(), 10)
        assert await wait_for(lambda: wa_call.id not in rig.rt.calls)
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["result"] == "no answer"
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_unrouted_whatsapp_call_is_left_alone(tmp_path):
    rig = await Rig().start(tmp_path, {})
    try:
        wa_call = rig.rt.simulate_incoming("4930999999")         # not listed, no catch-all bridge
        await asyncio.sleep(0.5)
        assert not rig.incoming and rig.rt.calls[wa_call.id].state == "incoming"
        rig.store.config.wa_accounts[0].unrouted = "reject"
        other = rig.rt.simulate_incoming("4930888888")
        assert await wait_for(lambda: other.id not in rig.rt.calls)
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_star_cancels_a_ringing_whatsapp_call(tmp_path):
    contacts = [BridgeContact(number="491702222222", name="Slow"), BridgeContact(number="4917011", name="X")]
    rig = await Rig().start(tmp_path, {"contacts": contacts, "dial_timeout": 60})
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.3)
        await call.send_dtmf("1")
        assert await wait_for(lambda: rig.rt.active_call() is not None, 5)
        await asyncio.sleep(1.5)                       # "Calling Slow." then ring-back
        await call.send_dtmf("*")
        assert await wait_for(lambda: rig.rt.active_call() is None, 5)
        assert await wait_for(lambda: any(s.phase == "menu" for s in rig.engine.sessions.values()), 5)
        await call.hangup()
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_dial_a_number_reads_digits_but_shows_plus_number(tmp_path):
    rig = await Rig().start(tmp_path, {"dial_number": True, "country_code": "49"})
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.3)
        await call.send_dtmf("0")
        await asyncio.sleep(0.3)
        for d in "01701119999#":                      # national format -> 491701119999 (not on WhatsApp)
            await call.send_dtmf(d)
        assert await wait_for(lambda: any("not on WhatsApp" in s.result for s in rig.engine.sessions.values()), 5)
        session = next(iter(rig.engine.sessions.values()))
        assert session.peer_name == "+491701119999"
        await call.hangup()
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["wa_peer"] == "+491701119999"
    finally:
        await rig.stop()
