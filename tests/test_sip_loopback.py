import asyncio

import pytest

from wa2sip.media.rtp import PortAllocator
from wa2sip.sip.auth import parse_challenge
from wa2sip.sip.message import SipMessage
from wa2sip.sip.stack import SipStack
from wa2sip.sip.ua import Account, AccountConfig, UserAgent

from .conftest import free_port


async def make_pair():
    pa, pb = free_port(), free_port()
    ua_a = UserAgent(pa, PortAllocator(30000, 30019), advertise_ip="127.0.0.1")
    ua_b = UserAgent(pb, PortAllocator(30020, 30039), advertise_ip="127.0.0.1")
    await ua_a.start()
    await ua_b.start()
    acc_a = Account(ua_a, AccountConfig(id="a", server="127.0.0.1", port=pb, username="a", password="x", contact_user="a"))
    acc_b = Account(ua_b, AccountConfig(id="b", server="127.0.0.1", port=pa, username="b", password="x", contact_user="b"))
    ua_a.accounts["a"] = acc_a
    ua_b.accounts["b"] = acc_b
    return ua_a, ua_b, acc_a, pb


@pytest.mark.asyncio
async def test_call_answer_media_and_bye():
    ua_a, ua_b, acc_a, pb = await make_pair()
    incoming = []

    async def on_incoming(call):
        incoming.append(call)
        call.ring()
        await call.answer()
    ua_b.on_incoming = on_incoming
    try:
        call = await acc_a.dial(f"b@127.0.0.1:{pb}")
        await asyncio.wait_for(call.answered.wait(), 5)
        callee = incoming[0]
        assert call.state == callee.state == "active"
        assert call.codec == callee.codec == "PCMA"
        got = []
        callee.rtp.on_packet = got.append
        call.rtp.send(call.negotiated.remote_pt, b"\xd5" * 160, 160)
        await asyncio.sleep(0.1)
        assert got and got[0].payload == b"\xd5" * 160
        await asyncio.wait_for(callee._acked.wait(), 2)   # ACK for the 200 OK arrived
        await call.hangup()
        await asyncio.wait_for(callee.ended.wait(), 5)
        assert callee.end_reason == "remote hangup" and call.state == "ended"
        assert not ua_a.calls and not ua_b.calls
    finally:
        await ua_a.stop()
        await ua_b.stop()


@pytest.mark.asyncio
async def test_cancel_while_ringing():
    ua_a, ua_b, acc_a, pb = await make_pair()
    incoming = []

    async def on_incoming(call):
        incoming.append(call)
        call.ring()
    ua_b.on_incoming = on_incoming
    try:
        call = await acc_a.dial(f"b@127.0.0.1:{pb}")
        for _ in range(50):
            if incoming:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await call.hangup()
        await asyncio.wait_for(incoming[0].ended.wait(), 5)
        assert incoming[0].end_reason == "caller cancelled"
        await asyncio.wait_for(call.ended.wait(), 5)
        assert call.end_reason.startswith("487")
    finally:
        await ua_a.stop()
        await ua_b.stop()


@pytest.mark.asyncio
async def test_busy_reject():
    ua_a, ua_b, acc_a, pb = await make_pair()

    async def on_incoming(call):
        call.reject(486, "Busy Here")
    ua_b.on_incoming = on_incoming
    try:
        call = await acc_a.dial(f"b@127.0.0.1:{pb}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert call.end_reason == "486 Busy Here"
    finally:
        await ua_a.stop()
        await ua_b.stop()


@pytest.mark.asyncio
async def test_register_with_digest_challenge():
    """A fake registrar challenges once, then checks the digest and grants 120 s."""
    reg_port = free_port()
    registrar = SipStack(reg_port, "127.0.0.1")
    seen = []

    async def handle(req: SipMessage, addr):
        seen.append(req)
        auth = req.get("Authorization")
        if not auth:
            registrar.respond(req, 401, "Unauthorized",
                              [("WWW-Authenticate", 'Digest realm="asterisk", nonce="n1", algorithm=MD5, qop="auth"')])
            return
        params = parse_challenge(auth)
        assert params["username"] == "1008" and params["realm"] == "asterisk" and params["nc"] == "00000001"
        contact = req.get("Contact")
        registrar.respond(req, 200, "OK", [("Contact", f"{contact};expires=120")])
    registrar.request_handler = handle
    await registrar.start()
    ua = UserAgent(free_port(), PortAllocator(30040, 30059), advertise_ip="127.0.0.1")
    await ua.start()
    try:
        await ua.set_accounts([AccountConfig(id="p", server="127.0.0.1", port=reg_port, username="1008",
                                             password="secret", contact_user="c2s-x")])
        acc = ua.accounts["p"]
        for _ in range(100):
            if acc.state == "registered":
                break
            await asyncio.sleep(0.02)
        assert acc.state == "registered"
        assert acc.registered_until is not None
        assert [bool(r.get("Authorization")) for r in seen] == [False, True]
        assert "c2s-x@127.0.0.1" in seen[-1].get("Contact")
    finally:
        await ua.stop()
        registrar.close()


@pytest.mark.asyncio
async def test_second_hangup_while_cancelling_does_not_wait():
    """Two hang-ups of a ringing call (e.g. two cleanup paths) send one CANCEL and return fast."""
    ua_a, ua_b, acc_a, pb = await make_pair()
    incoming = []

    async def on_incoming(call):
        incoming.append(call)
        call.ring()
    ua_b.on_incoming = on_incoming
    try:
        call = await acc_a.dial(f"b@127.0.0.1:{pb}")
        for _ in range(50):
            if incoming:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        sent = []
        real = call._send_cancel

        async def counting():
            sent.append(1)
            await real()
        call._send_cancel = counting
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(call.hangup(), call.hangup())
        assert len(sent) == 1                          # Asterisk doesn't answer a repeated CANCEL
        assert loop.time() - t0 < 2.0
        assert call.state == "ended" and call.end_reason.startswith("487")
    finally:
        await ua_a.stop()
        await ua_b.stop()
