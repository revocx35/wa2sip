"""Integration API v1 (telerouter): token auth, targets, one-time call addresses, direct calls both ways."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from wa2sip import integration
from wa2sip.media import g711
from wa2sip.models import BridgeContact
from wa2sip.settings import Settings
from wa2sip.sip.ua import Account, AccountConfig
from wa2sip.wa.fake import CONTACTS
from wa2sip.web.app import create_app

from .conftest import free_port
from .test_engine import Rig, send_audio, tone_power, wait_for


# -- web API ----------------------------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                 rtp_port_min=31040, rtp_port_max=31059, https_port=0)
    with TestClient(create_app(s), base_url="https://testserver") as c:
        assert c.post("/api/setup", json={"password": "secret1234"}).status_code == 200
        c.post("/api/wa", json={"name": "Phone"})
        yield c


def test_integration_token_is_shown_once_and_stored_hashed(client):
    st = client.get("/api/settings/integration").json()
    assert st["token_set"] is False
    tok = client.post("/api/settings/integration-token").json()["token"]
    assert tok.startswith("w2s_") and len(tok) > 40
    st = client.get("/api/settings/integration").json()
    assert st["token_set"] is True and "token" not in st
    cfg = client.app.state.store.config.settings
    assert cfg.integration_token_hash == integration.token_hash(tok) and tok not in cfg.model_dump_json()


def test_integration_api_needs_its_token_and_https(client, tmp_path):
    tok = client.post("/api/settings/integration-token").json()["token"]
    client.post("/api/logout")
    assert client.get("/api/integration/v1/info").status_code == 401
    assert client.get("/api/integration/v1/info", headers={"Authorization": "Bearer w2s_wrong"}).status_code == 401
    ok = client.get("/api/integration/v1/info", headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200 and ok.json()["app"] == "wa2sip"
    # the integration token reaches nothing else
    assert client.get("/api/wa", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    # plain HTTP is refused
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                 rtp_port_min=31060, rtp_port_max=31079, https_port=0)
    with TestClient(create_app(s), base_url="http://testserver") as plain:
        assert plain.get("/api/integration/v1/info", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    # revoked: refused
    client.post("/api/login", json={"password": "secret1234"})
    client.delete("/api/settings/integration-token")
    assert client.get("/api/integration/v1/info", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_targets_calls_and_inbound(client):
    tok = client.post("/api/settings/integration-token").json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    for _ in range(50):
        targets = client.get("/api/integration/v1/targets", headers=h).json()["targets"]
        if targets:
            break
        import time
        time.sleep(0.1)
    names = {t["name"] for t in targets}
    assert {c["name"] for c in CONTACTS} <= names
    t = next(t for t in targets if t["name"] == CONTACTS[0]["name"])
    assert t["kind"] == "whatsapp" and t["number"] == CONTACTS[0]["number"]
    r = client.post("/api/integration/v1/calls", headers=h, json={"target": t["id"]}).json()
    assert r["sip_uri"].startswith("sip:tr") and r["expires_in"] == 30
    assert client.post("/api/integration/v1/calls", headers=h, json={"number": "12"}).status_code == 422
    assert client.put("/api/integration/v1/inbound", headers=h,
                      json={"sip_uri": "http://evil", "enabled": True}).status_code == 422
    assert client.put("/api/integration/v1/inbound", headers=h,
                      json={"sip_uri": "sip:in-abcdefgh@192.168.1.5:5080", "enabled": True}).status_code == 200
    st = client.app.state.store.config.settings
    assert st.telerouter_enabled and st.telerouter_uri.endswith(":5080")


# -- SIP: telerouter -> WhatsApp -------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_call_rings_answers_and_bridges(tmp_path):
    rig = await Rig().start(tmp_path, {}, answer_delay=1.0)
    try:
        t = rig.engine.tickets.create("w", CONTACTS[0]["id"], CONTACTS[0]["name"], "127.0.0.1")
        call = await rig.phone.dial(f"{t.user}@127.0.0.1:{rig.settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 6)      # 200 only once WhatsApp answered
        rx = bytearray()
        call.rtp.on_packet = lambda pkt: rx.extend(pkt.payload) if pkt.pt == call.negotiated.remote_pt else None
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 4)
        await send_audio(call, g711.tone(call.codec, 700, 0.8, -10) + g711.silence(call.codec, 4000))
        assert tone_power(bytes(rx), call.codec, 700) > -30            # echoed by the fake WhatsApp peer
        await call.hangup()
        assert await wait_for(lambda: not rig.engine.sessions)
        assert rig.rt.active_call() is None
        assert rig.store.history[-1]["direction"] == "integration-to-wa"
        # a ticket works once
        again = await rig.phone.dial(f"{t.user}@127.0.0.1:{rig.settings.sip_port}")
        await asyncio.wait_for(again.ended.wait(), 5)
        assert "404" in again.end_reason
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_direct_call_failures_map_to_sip_statuses(tmp_path):
    rig = await Rig().start(tmp_path, {}, answer_delay=0.5)
    try:
        for number, status in (("491701119999", "404"), ("491701111111", "486"), ("491701110000", "603")):
            t = rig.engine.tickets.create("w", number, number, "127.0.0.1")
            call = await rig.phone.dial(f"{t.user}@127.0.0.1:{rig.settings.sip_port}")
            await asyncio.wait_for(call.ended.wait(), 8)
            assert status in call.end_reason, (number, call.end_reason)
            assert await wait_for(lambda: not rig.engine.sessions)
        # a ticket bound to another IP is refused
        t = rig.engine.tickets.create("w", CONTACTS[0]["id"], "x", "10.9.9.9")
        call = await rig.phone.dial(f"{t.user}@127.0.0.1:{rig.settings.sip_port}")
        await asyncio.wait_for(call.ended.wait(), 5)
        assert "404" in call.end_reason
    finally:
        await rig.stop()


# -- SIP: WhatsApp -> telerouter --------------------------------------------------------------------------------
async def _telerouter_rig(tmp_path, **bridge_kw):
    rig = await Rig().start(tmp_path, bridge_kw)
    tr = Account(rig.ua, AccountConfig(id="tr", server="127.0.0.1", port=rig.settings.sip_port, username="tr",
                                       password="", contact_user="in-telerouter"))
    rig.ua.accounts["tr"] = tr
    st = rig.store.config.settings
    st.telerouter_uri = f"sip:in-telerouter@127.0.0.1:{rig.phone_port}"
    st.telerouter_enabled = True
    return rig


@pytest.mark.asyncio
async def test_incoming_whatsapp_call_rings_telerouter(tmp_path):
    rig = await _telerouter_rig(tmp_path, contacts=[])
    try:
        rig.rt.simulate_incoming(CONTACTS[2]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        call = rig.incoming[0]
        assert call.account.cfg.id == "tr"
        assert call.invite.get("X-Telerouter-Target") == f"w:{CONTACTS[2]['number']}@c.us"
        assert call.remote_user == "+" + CONTACTS[2]["number"]
        await call.answer()
        assert await wait_for(lambda: any(s.phase == "connected" for s in rig.engine.sessions.values()), 5)
        await call.hangup()
        assert await wait_for(lambda: not rig.engine.sessions)
        assert rig.rt.active_call() is None
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_explicitly_bridged_contact_keeps_its_bridge(tmp_path):
    rig = await _telerouter_rig(tmp_path, contacts=[BridgeContact(wa_id=CONTACTS[0]["id"], number=CONTACTS[0]["number"],
                                                                  name="Alice")])
    try:
        rig.rt.simulate_incoming(CONTACTS[0]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        assert rig.incoming[0].account.cfg.id == "2001"            # rang the bridge's extension
    finally:
        await rig.stop()


@pytest.mark.asyncio
async def test_catch_all_bridge_is_the_fallback_when_telerouter_declines(tmp_path):
    rig = await _telerouter_rig(tmp_path, contacts=[], all_contacts=True)
    try:
        rig.rt.simulate_incoming(CONTACTS[3]["number"])
        assert await wait_for(lambda: rig.incoming, 5)
        first = rig.incoming[0]
        assert first.account.cfg.id == "tr"
        first.reject(603, "Decline")
        assert await wait_for(lambda: len(rig.incoming) > 1, 5)
        assert rig.incoming[1].account.cfg.id == "2001"
    finally:
        await rig.stop()
