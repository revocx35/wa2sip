"""Bridge PINs: validation, brute-force lock, and both call directions."""

import asyncio

import pytest

from wa2sip.models import Bridge
from wa2sip.pin import FREE_FAILURES, PinGuard
from wa2sip.sip.ua import Account, AccountConfig
from wa2sip.wa.fake import CONTACTS

from .test_engine import Rig, wait_for


def test_pin_validation_and_directions():
    b = Bridge(wa_account_id="w", extension_id="e", pin="0042")
    assert b.pin_required("outbound") and b.pin_required("inbound")
    b = Bridge(wa_account_id="w", extension_id="e", pin="123456", pin_inbound=False)
    assert b.pin_required("outbound") and not b.pin_required("inbound")
    assert not Bridge(wa_account_id="w", extension_id="e").pin_required("outbound")
    for bad in ("12", "12a4", "1" * 17):
        with pytest.raises(ValueError):
            Bridge(wa_account_id="w", extension_id="e", pin=bad)


def test_pin_guard_locks_after_repeated_wrong_pins():
    now = [1000.0]
    g = PinGuard(clock=lambda: now[0])
    assert g.check("b", "1234", "1234")
    for _ in range(FREE_FAILURES - 1):
        assert not g.check("b", "0000", "1234")
    assert g.locked_for("b") == 0
    assert not g.check("b", "0000", "1234")             # the 10th wrong PIN locks the bridge
    assert 59 < g.locked_for("b") <= 60
    assert not g.check("b", "1234", "1234")             # even the right PIN is refused while locked
    assert g.check("other", "1", "1")                   # other bridges are not affected
    now[0] += 61
    assert not g.check("b", "0000", "1234")             # still failing: the lock doubles
    assert 119 < g.locked_for("b") <= 120
    now[0] += 121
    assert g.check("b", "1234", "1234")                 # the right PIN resets the count
    assert not g.check("b", "0000", "1234") and g.locked_for("b") == 0


async def keys(call, digits: str):
    for d in digits:
        await call.send_dtmf(d)


@pytest.mark.asyncio
async def test_outbound_pin_unlocks_the_menu(tmp_path):
    rig = await Rig().start(tmp_path, {"pin": "2468"})
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.6)
        assert any(s.phase == "pin" for s in rig.engine.sessions.values())
        await keys(call, "2468#")
        assert await wait_for(lambda: any(s.phase == "menu" for s in rig.engine.sessions.values()), 5)
        await call.send_dtmf("1")
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 8)
        await call.hangup()
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_outbound_wrong_pin_hangs_up_without_calling_whatsapp(tmp_path):
    rig = await Rig().start(tmp_path, {"pin": "2468", "pin_attempts": 1})
    try:
        call, rx = await rig.call_bridge()
        await asyncio.sleep(0.6)
        await keys(call, "1111#")
        await asyncio.wait_for(call.ended.wait(), 15)          # "Wrong PIN." "Goodbye." then BYE
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["result"] == "wrong PIN"
        assert rig.rt.active_call() is None and not rig.rt.calls
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_inbound_pin_before_whatsapp_is_answered(tmp_path):
    rig = await Rig().start(tmp_path, {"pin": "2468"})
    try:
        wa_call = rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        call = rig.incoming.pop()
        await call.answer()
        await asyncio.sleep(1.0)
        assert rig.rt.calls[wa_call.id].state == "incoming"    # not answered before the PIN
        await keys(call, "2468#")
        assert await wait_for(lambda: wa_call.id in rig.rt.calls and rig.rt.calls[wa_call.id].state == "active", 8)
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 8)
        await call.hangup()
        assert await wait_for(lambda: wa_call.id not in rig.rt.calls)
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_inbound_wrong_pin_declines_or_leaves_ringing(tmp_path):
    rig = await Rig().start(tmp_path, {"pin": "2468", "pin_attempts": 1, "reject_unanswered": False})
    try:
        wa_call = rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        call = rig.incoming.pop()
        await call.answer()
        await asyncio.sleep(0.5)
        await keys(call, "1357#")
        await asyncio.wait_for(call.ended.wait(), 15)
        assert await wait_for(lambda: rig.store.history)
        assert rig.store.history[-1]["result"] == "wrong PIN"
        assert rig.rt.calls[wa_call.id].state == "incoming"     # left ringing on the phone
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_other_extensions_keep_ringing_while_one_enters_a_pin(tmp_path):
    rig = await Rig().start(tmp_path, {"pin": "2468", "pin_attempts": 1})
    rig.store.config.bridges[0].ring_targets = ["2001", "2002"]
    rig.ua.accounts["2002"] = Account(rig.ua, AccountConfig(id="2002", server="127.0.0.1", port=rig.settings.sip_port,
                                                            username="2002", password="", contact_user="2002"))
    try:
        wa_call = rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: len(rig.incoming) == 2, 5)
        first, second = sorted(rig.incoming, key=lambda c: c.account.cfg.username)
        await first.answer()                                   # 2001 picks up and gets the PIN wrong
        await asyncio.sleep(0.5)
        await keys(first, "0000#")
        await asyncio.wait_for(first.ended.wait(), 15)
        assert second.state == "ringing"                       # 2002 kept ringing all along
        await second.answer()
        await asyncio.sleep(0.5)
        await keys(second, "2468#")
        assert await wait_for(lambda: wa_call.id in rig.rt.calls and rig.rt.calls[wa_call.id].state == "active", 8)
        session = next(iter(rig.engine.sessions.values()))
        assert await wait_for(lambda: session.phase == "connected", 8)
        assert session.answered_by == "2002"
        await second.hangup()
    finally:
        await rig.stop()
