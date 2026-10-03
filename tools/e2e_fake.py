#!/usr/bin/env python3
"""End-to-end test: a real PBX (tools/test-pbx) + wa2sip with simulated WhatsApp accounts.

    docker build -t wa2sip-test-pbx tools/test-pbx && docker run -d --rm --name wa2sip-pbx --network host wa2sip-test-pbx
    docker run -d --rm --name wa2sip-e2e --network host -e WA2SIP_WA_DRIVER=fake \
        -e WA2SIP_ADMIN_PASSWORD=e2e-password-1 wa2sip:dev
    docker run --rm --network host -v $PWD:/src -w /src wa2sip:dev python tools/e2e_fake.py

What it checks, through Asterisk:
  1. PBX -> WhatsApp: phone 2001 calls extension 1009, hears the menu, presses 2, the simulated
     WhatsApp contact answers and echoes; a tone sent by the phone must come back.
  2. Dial-a-number from phone 2002, which only sends in-band key tones and only speaks u-law
     (Asterisk detects the tones and sends RFC 4733 to wa2sip), to a number that is not on
     WhatsApp: the caller hears the "not on WhatsApp" prompt.
  3. WhatsApp -> PBX: a simulated WhatsApp call rings 2001 (registered by this script), which
     answers, hears the announcement and then the echoed tone; hanging up ends the WhatsApp call.
  4. With a bridge PIN: calling 1009 needs the PIN before the menu, and answering a WhatsApp call
     needs it before WhatsApp is answered.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wa2sip.media import g711  # noqa: E402
from wa2sip.media.dtmf import HIGH, KEYS, LOW  # noqa: E402
from wa2sip.media.rtp import PortAllocator  # noqa: E402
from wa2sip.sip.ua import Account, AccountConfig, UserAgent  # noqa: E402

PBX = "127.0.0.1"
PBX_PORT = 5160


def goertzel_db(payload: bytes, codec: str, freq: float) -> float:
    dec = g711.DECODE[codec]
    xs = [dec[b] for b in payload]
    k = 2 * math.cos(2 * math.pi * freq / 8000)
    s1 = s2 = 0.0
    for x in xs:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - k * s1 * s2
    return 10 * math.log10(max(p, 1e-9) / max(len(xs), 1) ** 2 / 32768 ** 2 * 4) if xs else -120


def dtmf_audio(digit: str, codec: str, ms: int = 120) -> bytes:
    row = next(i for i, r in enumerate(KEYS) if digit in r)
    f1, f2 = LOW[row], HIGH[KEYS[row].index(digit)]
    n = 8 * ms
    samples = [int(7000 * (math.sin(2 * math.pi * f1 * i / 8000) + math.sin(2 * math.pi * f2 * i / 8000)))
               for i in range(n)]
    return g711.encode_pcm(samples, codec) + g711.silence(codec, 8 * 80)


class Phone:
    def __init__(self, user: str, password: str, sip_port: int, rtp: int):
        self.ua = UserAgent(sip_port, PortAllocator(rtp, rtp + 40), user_agent=f"e2e-phone-{user}")
        self.user = user
        self.password = password
        self.incoming: list = []

    async def start(self, register: bool) -> None:
        await self.ua.start()
        cfg = AccountConfig(id=self.user, server=PBX, port=PBX_PORT, username=self.user,
                            password=self.password, contact_user=self.user, expires=120)
        if register:
            await self.ua.set_accounts([cfg])
            acc = self.ua.accounts[self.user]
            for _ in range(100):
                if acc.state == "registered":
                    break
                await asyncio.sleep(0.05)
            assert acc.state == "registered", f"{self.user} did not register: {acc.error}"
        else:
            self.ua.accounts[self.user] = Account(self.ua, cfg)

        async def on_incoming(call):
            self.incoming.append(call)
            call.ring()
        self.ua.on_incoming = on_incoming

    @property
    def account(self) -> Account:
        return self.ua.accounts[self.user]

    async def stop(self) -> None:
        await self.ua.stop()


class Recorder:
    def __init__(self, call):
        self.call = call
        self.rx = bytearray()
        call.rtp.on_packet = self._on

    def _on(self, pkt):
        n = self.call.negotiated
        if n and pkt.pt == n.remote_pt:
            self.rx.extend(pkt.payload)

    def mark(self) -> int:
        return len(self.rx)

    def level_since(self, mark: int) -> float:
        return g711.level_dbfs(bytes(self.rx[mark:]), self.call.codec) if len(self.rx) > mark else -96.0


async def stream(call, audio: bytes) -> None:
    loop = asyncio.get_running_loop()
    t = loop.time()
    pt = call.negotiated.remote_pt
    for i in range(0, len(audio), 160):
        call.rtp.send(pt, audio[i:i + 160].ljust(160, bytes([g711.SILENCE[call.codec]])), 160)
        t += 0.02
        await asyncio.sleep(max(0.0, t - loop.time()))


async def silence_for(call, seconds: float) -> None:
    await stream(call, g711.silence(call.codec, int(8000 * seconds)))


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        raise SystemExit(1)


async def setup(api: httpx.AsyncClient) -> dict:
    for path in ("bridges", "extensions", "pbxs", "wa"):
        for item in (await api.get(f"/api/{path}")).json():
            await api.delete(f"/api/{path}/{item['id']}")
    pbx = (await api.post("/api/pbxs", json={"name": "test pbx", "host": PBX, "port": PBX_PORT})).json()
    ext = (await api.post("/api/extensions", json={"pbx_id": pbx["id"], "username": "1009",
                                                   "password": "Test-1009-pw"})).json()
    wa = (await api.post("/api/wa", json={"name": "Simulated"})).json()
    r = await api.put("/api/settings", json={"default_voice": "en-us", "default_speed": 170})
    assert r.status_code == 200, r.text
    contacts = (await api.get(f"/api/wa/{wa['id']}/contacts")).json()
    pick = [{"wa_id": c["id"], "number": c["number"], "name": c["name"]} for c in contacts[:3]]
    r = await api.post("/api/bridges", json={
        "name": "e2e", "wa_account_id": wa["id"], "extension_id": ext["id"], "contacts": pick,
        "ring_targets": ["2001"], "dial_number": True, "ivr_greeting": "Hello.", "ring_timeout": 20})
    assert r.status_code == 200, r.text
    bridge = r.json()
    for _ in range(100):
        st = (await api.get("/api/status")).json()
        if st["extensions"][ext["id"]]["state"] == "registered":
            break
        await asyncio.sleep(0.1)
    check(st["extensions"][ext["id"]]["state"] == "registered", "wa2sip registered extension 1009")
    return {"wa": wa, "bridge": bridge, "contacts": contacts}


async def test_outbound(api, phone: Phone, ctx: dict) -> None:
    print("PBX -> WhatsApp (menu, key 2, echo)")
    menu = ctx["bridge"]["menu"]
    print("  menu:", ctx["bridge"]["menu_text"])
    call = await phone.account.dial("1009")
    await asyncio.wait_for(call.answered.wait(), 10)
    rec = Recorder(call)
    check(call.state == "active", f"1009 answered ({call.codec})")
    m = rec.mark()
    await silence_for(call, 2.0)
    check(rec.level_since(m) > -45, f"menu is audible ({rec.level_since(m):.1f} dBFS)")
    await call.send_dtmf(menu[1]["code"])
    await silence_for(call, 4.0)            # "Calling Bob", ringback, simulated answer after 2 s
    calls = (await api.get("/api/calls")).json()["active"]
    check(len(calls) == 1 and calls[0]["phase"] == "connected", f"bridged to WhatsApp ({calls[0]['phase']})")
    check(calls[0]["peer"]["name"] == menu[1]["name"], f"called {calls[0]['peer']['name']}")
    m = rec.mark()
    await stream(call, g711.tone(call.codec, 700, 1.5, -10))
    await silence_for(call, 1.0)
    echo = bytes(rec.rx[m:])
    lvl = goertzel_db(echo, call.codec, 700)
    check(lvl > -30, f"700 Hz tone echoed back by the WhatsApp side ({lvl:.1f} dB)")
    await call.hangup()
    await asyncio.sleep(1.0)
    calls = (await api.get("/api/calls")).json()
    check(not calls["active"], "session cleaned up after hang-up")
    last = calls["history"][0]
    check(last["direction"] == "pbx-to-wa" and last["result"] == "connected" and last["duration"] >= 2,
          f"history: {last['result']}, {last['duration']} s")
    st = (await api.get(f"/api/wa/{ctx['wa']['id']}/status")).json()
    check(not st["calls"], "WhatsApp call was hung up too")


async def test_dial_number_inband(api, phone: Phone, ctx: dict) -> None:
    print("PBX -> WhatsApp: dial-a-number from an in-band-DTMF, u-law phone; number not on WhatsApp")
    call = await phone.account.dial("1009")
    await asyncio.wait_for(call.answered.wait(), 10)
    rec = Recorder(call)
    check(call.codec == "PCMU", f"answered with {call.codec} (2002 is u-law only)")
    await silence_for(call, 1.0)
    await stream(call, dtmf_audio("0", call.codec))
    await silence_for(call, 1.5)
    for d in "4915112349999#":
        await stream(call, dtmf_audio(d, call.codec, 90))
    m = rec.mark()
    await silence_for(call, 3.0)
    check(rec.level_since(m) > -45, f"a prompt follows the number ({rec.level_since(m):.1f} dBFS)")
    await call.hangup()
    await asyncio.sleep(1.0)
    last = (await api.get("/api/calls")).json()["history"][0]
    check("not on WhatsApp" in last["result"], f"history: {last['result']}")


async def test_inbound(api, phone: Phone, ctx: dict) -> None:
    print("WhatsApp -> PBX (ring 2001, announcement, echo)")
    alice = ctx["contacts"][0]
    r = await api.post(f"/api/wa/{ctx['wa']['id']}/simulate-call", json={"number": alice["number"]})
    check(r.status_code == 200, "simulated incoming WhatsApp call")
    for _ in range(100):
        if phone.incoming:
            break
        await asyncio.sleep(0.05)
    check(bool(phone.incoming), "extension 2001 rings")
    call = phone.incoming.pop()
    check(alice["name"] in call.remote_display, f"caller name shown: {call.remote_display!r}")
    await asyncio.sleep(0.5)
    await call.answer()
    rec = Recorder(call)
    m = rec.mark()
    await silence_for(call, 2.5)
    check(rec.level_since(m) > -45, f"announcement is audible ({rec.level_since(m):.1f} dBFS)")
    st = (await api.get(f"/api/wa/{ctx['wa']['id']}/status")).json()
    check(st["calls"] and st["calls"][0]["state"] == "active", "WhatsApp call accepted")
    m = rec.mark()
    await stream(call, g711.tone(call.codec, 900, 1.5, -10))
    await silence_for(call, 1.0)
    lvl = goertzel_db(bytes(rec.rx[m:]), call.codec, 900)
    check(lvl > -30, f"900 Hz tone echoed back by the WhatsApp side ({lvl:.1f} dB)")
    await call.hangup()
    await asyncio.sleep(1.0)
    st = (await api.get(f"/api/wa/{ctx['wa']['id']}/status")).json()
    check(not st["calls"], "WhatsApp call ended with the PBX call")
    last = (await api.get("/api/calls")).json()["history"][0]
    check(last["direction"] == "wa-to-pbx" and last["result"] == "connected", f"history: {last['result']}")

    print("WhatsApp -> PBX: caller hangs up while ringing")
    await api.post(f"/api/wa/{ctx['wa']['id']}/simulate-call", json={"number": alice["number"]})
    for _ in range(100):
        if phone.incoming:
            break
        await asyncio.sleep(0.05)
    check(bool(phone.incoming), "extension 2001 rings again")
    ringing = phone.incoming.pop()
    await api.post(f"/api/wa/{ctx['wa']['id']}/simulate-hangup")
    await asyncio.wait_for(ringing.ended.wait(), 5)
    check(ringing.state == "ended", f"ringing stopped ({ringing.end_reason})")


async def test_pin(api, phone: Phone, ctx: dict) -> None:
    print("PIN on both directions")
    b = ctx["bridge"]
    r = await api.put(f"/api/bridges/{b['id']}", json={"pin": "4711"})
    check(r.status_code == 200, "bridge PIN set")
    call = await phone.account.dial("1009")
    await asyncio.wait_for(call.answered.wait(), 10)
    await silence_for(call, 0.8)
    for d in "4711#":
        await call.send_dtmf(d)
    await silence_for(call, 1.0)
    active = (await api.get("/api/calls")).json()["active"]
    phase = next((x["phase"] for x in active if x["kind"] == "pbx-to-wa"), "no session")
    check(phase == "menu", f"PIN accepted, caller is in the menu ({phase})")
    await call.hangup()
    await asyncio.sleep(1.0)

    alice = ctx["contacts"][0]
    await api.post(f"/api/wa/{ctx['wa']['id']}/simulate-call", json={"number": alice["number"]})
    for _ in range(100):
        if phone.incoming:
            break
        await asyncio.sleep(0.05)
    incoming = phone.incoming.pop()
    await incoming.answer()
    await silence_for(incoming, 1.0)
    st = (await api.get(f"/api/wa/{ctx['wa']['id']}/status")).json()
    check(st["calls"][0]["state"] == "incoming", "WhatsApp not answered before the PIN")
    for d in "4711#":
        await incoming.send_dtmf(d)
    await silence_for(incoming, 1.5)
    st = (await api.get(f"/api/wa/{ctx['wa']['id']}/status")).json()
    check(st["calls"] and st["calls"][0]["state"] == "active", "WhatsApp answered after the PIN")
    await incoming.hangup()
    await asyncio.sleep(1.0)
    await api.put(f"/api/bridges/{b['id']}", json={"pin": ""})


async def main(args) -> int:
    logging.basicConfig(level=logging.WARNING)
    async with httpx.AsyncClient(base_url=args.url, timeout=20) as api:
        r = await api.post("/api/login", json={"password": args.password})
        assert r.status_code == 200, r.text
        ctx = await setup(api)
        p1 = Phone("2001", "Test-2001-pw", 5170, 17500)
        p2 = Phone("2002", "Test-2002-pw", 5171, 17560)
        await p1.start(register=True)
        await p2.start(register=False)
        try:
            await test_outbound(api, p1, ctx)
            await test_dial_number_inband(api, p2, ctx)
            await test_inbound(api, p1, ctx)
            await test_pin(api, p1, ctx)
        finally:
            await p1.stop()
            await p2.stop()
    print("ALL OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8092")
    ap.add_argument("--password", default="e2e-password-1")
    sys.exit(asyncio.run(main(ap.parse_args())))
