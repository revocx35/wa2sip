import json
import time

import pytest
from fastapi.testclient import TestClient

from wa2sip.settings import Settings
from wa2sip.web.app import create_app

from .conftest import free_port


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                 rtp_port_min=31000, rtp_port_max=31019)
    with TestClient(create_app(s)) as c:
        c.data_dir = tmp_path
        assert c.post("/api/setup", json={"password": "secret1234"}).status_code == 200
        yield c


def test_setup_and_auth(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                 rtp_port_min=31020, rtp_port_max=31039)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/session").json()["setup_required"] is True
        assert c.get("/api/pbxs").status_code == 401
        assert c.post("/api/setup", json={"password": "short"}).status_code == 400
        assert c.post("/api/setup", json={"password": "secret1234"}).status_code == 200
        assert c.post("/api/setup", json={"password": "again12345"}).status_code == 409
        assert c.get("/api/pbxs").status_code == 200
        c.post("/api/logout")
        assert c.get("/api/pbxs").status_code == 401
        assert c.post("/api/login", json={"password": "wrong-password"}).status_code == 401
        assert c.post("/api/login", json={"password": "secret1234"}).status_code == 200


def test_pbx_extension_crud_and_secrets(client):
    pbx = client.post("/api/pbxs", json={"name": "FreePBX", "host": "127.0.0.1", "port": 9}).json()
    assert client.post("/api/pbxs", json={"host": " "}).status_code == 422
    ext = client.post("/api/extensions", json={"pbx_id": pbx["id"], "username": "1009",
                                               "password": "s3cret"}).json()
    assert ext["password"] == "" and ext["password_set"] and ext["contact_user"].startswith("wa-")
    assert client.post("/api/extensions", json={"pbx_id": pbx["id"], "username": "1009"}).status_code == 409
    assert client.post("/api/extensions", json={"pbx_id": "nope", "username": "1010"}).status_code == 422
    # an empty secret on update keeps the stored one
    client.put(f"/api/extensions/{ext['id']}", json={"display_name": "WhatsApp", "password": ""})
    stored = json.loads((client.data_dir / "config.json").read_text())["extensions"][0]
    assert stored["display_name"] == "WhatsApp" and stored["password"] == "s3cret"
    assert client.delete(f"/api/pbxs/{pbx['id']}").status_code == 409        # extension still uses it
    st = client.get("/api/status").json()
    assert st["extensions"][ext["id"]]["state"] in ("registering", "failed", "registered")


def make_bridge(client, **kw):
    pbx = client.post("/api/pbxs", json={"host": "127.0.0.1", "port": 9}).json()
    ext = client.post("/api/extensions", json={"pbx_id": pbx["id"], "username": kw.pop("ext", "1009")}).json()
    wa = client.post("/api/wa", json={"name": "Phone"}).json()
    body = {"wa_account_id": wa["id"], "extension_id": ext["id"], "ring_targets": ["2001"], **kw}
    return wa, ext, client.post("/api/bridges", json=body)


def test_whatsapp_account_contacts_and_bridge(client):
    wa, ext, r = make_bridge(client, name="Family", contacts=[
        {"wa_id": "491701111001@c.us", "number": "491701111001", "name": "Alice"},
        {"number": "+49 170 1111002", "name": "Bob", "digit": "5"}], dial_number=True)
    assert r.status_code == 200, r.text
    b = r.json()
    assert [m["code"] for m in b["menu"]] == ["1", "5"]
    assert b["menu_text"].endswith("Press 0 to dial a phone number.")
    accounts = client.get("/api/wa").json()
    assert accounts[0]["status"]["state"] == "ready"
    contacts = client.get(f"/api/wa/{wa['id']}/contacts").json()
    assert contacts and all("number" in c for c in contacts)
    assert client.get(f"/api/wa/{wa['id']}/lookup", params={"number": "491701119999"}).status_code == 404
    assert client.get(f"/api/wa/{wa['id']}/lookup", params={"number": "491701111002"}).json()["name"]
    # the same contact can't ring through two bridges of one account
    pbx_id = client.get("/api/pbxs").json()[0]["id"]
    ext2 = client.post("/api/extensions", json={"pbx_id": pbx_id, "username": "1010"}).json()
    dup = client.post("/api/bridges", json={"wa_account_id": wa["id"], "extension_id": ext2["id"],
                                            "ring_targets": ["2002"], "contacts": [{"number": "491701111001"}]})
    assert dup.status_code == 409 and "already rings" in dup.json()["detail"]
    # an incoming route needs somewhere to ring
    r = client.post("/api/bridges", json={"wa_account_id": wa["id"], "extension_id": ext2["id"], "all_contacts": True})
    assert r.status_code == 422
    assert client.delete(f"/api/wa/{wa['id']}").status_code == 409            # bridge still uses it
    assert client.delete(f"/api/extensions/{ext['id']}").status_code == 409
    assert client.delete(f"/api/bridges/{b['id']}").status_code == 200
    assert client.delete(f"/api/wa/{wa['id']}").status_code == 200


def test_simulated_call_shows_up_in_status(client):
    wa, ext, r = make_bridge(client, all_contacts=True)
    assert r.status_code == 200
    call = client.post(f"/api/wa/{wa['id']}/simulate-call", json={"number": "4930123456"}).json()
    assert call["state"] == "incoming"
    for _ in range(50):
        acc = client.get(f"/api/wa/{wa['id']}/status").json()
        if acc["calls"]:
            break
        time.sleep(0.05)
    assert acc["calls"][0]["peer"]["number"] == "4930123456"


def test_settings_and_preview(client):
    r = client.put("/api/settings", json={"default_voice": "en-us", "default_speed": 180, "ringback_style": "us"})
    assert r.status_code == 200 and r.json()["ringback_style"] == "us"
    assert client.put("/api/settings", json={"ringback_style": "mars"}).status_code == 422
    wav = client.post("/api/tts/preview", json={"text": "Hello there"})
    assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
    menu = client.post("/api/tts/preview", json={"bridge": {"contacts": [{"number": "4911", "name": "A"}]}})
    assert menu.status_code == 200 and menu.content[:4] == b"RIFF"
    assert client.post("/api/tts/preview", json={"text": " "}).status_code == 422


def test_history(client):
    assert client.get("/api/calls").json() == {"active": [], "history": []}
    assert client.delete("/api/calls/history").status_code == 200
    assert client.post("/api/calls/nope/hangup").status_code == 404
