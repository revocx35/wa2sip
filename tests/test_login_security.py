"""Login hardening: brute-force throttle, trusted proxies, cross-site requests, cookie flags."""

import pytest
from fastapi.testclient import TestClient

from wa2sip.settings import Settings, parse_trusted_proxies
from wa2sip.web import auth
from wa2sip.web.app import create_app

from .conftest import free_port

PASSWORD = "correct horse"


def settings(tmp_path, rtp: int) -> Settings:
    return Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                    rtp_port_min=rtp, rtp_port_max=rtp + 19)


@pytest.fixture()
def app(tmp_path):
    app = create_app(settings(tmp_path, 31100))
    with TestClient(app) as c:
        assert c.post("/api/setup", json={"password": PASSWORD}).status_code == 200
        yield app


def client_at(app, ip):
    return TestClient(app, client=(ip, 50000))


def login(c, password, **headers):
    return c.post("/api/login", json={"password": password}, headers=headers)


def test_throttle_locks_ip_then_account():
    now = [1000.0]
    t = auth.LoginThrottle(clock=lambda: now[0])
    for _ in range(6):                      # 5 free failures; the 6th is checked, then locks
        t.begin("a")
    with pytest.raises(auth.Locked) as e:
        t.begin("a")
    assert 29 < e.value.retry_after <= 30
    t.begin("b")                            # other clients are not affected
    now[0] += 31
    t.begin("a")                            # lock expired; the next one doubles
    with pytest.raises(auth.Locked) as e:
        t.begin("a")
    assert 59 < e.value.retry_after <= 60
    t.success("a")
    t.begin("a")

    t = auth.LoginThrottle(clock=lambda: now[0])
    for i in range(51):                     # distributed guessing: one try per IP
        t.begin(f"10.0.{i // 250}.{i % 250}")
    with pytest.raises(auth.Locked):
        t.begin("192.0.2.1")                # a fresh IP is locked out too
    now[0] += 31
    t.begin("192.0.2.1")
    t.success("192.0.2.1")                  # the owner's successful login resets the account lock
    t.begin("192.0.2.2")


def test_login_is_throttled_per_client(app):
    attacker, owner = client_at(app, "203.0.113.5"), client_at(app, "198.51.100.20")
    assert [login(attacker, f"guess{i}").status_code for i in range(6)] == [401] * 6
    r = login(attacker, PASSWORD)           # even the right password is refused while locked
    assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= 30
    assert "try again" in r.json()["detail"]
    assert login(owner, PASSWORD).status_code == 200


def test_forwarded_for_counts_only_from_trusted_proxies(app):
    direct = client_at(app, "198.51.100.7")
    codes = [login(direct, f"g{i}", **{"X-Forwarded-For": f"10.9.0.{i}"}).status_code for i in range(7)]
    assert codes == [401] * 6 + [429]
    # through the reverse proxy (Docker bridge address): the proxy appends the real client IP,
    # so a spoofed entry in front of it changes nothing
    proxy = client_at(app, "172.17.0.2")
    spoofed = [login(proxy, f"g{i}", **{"X-Forwarded-For": f"10.8.0.{i}, 203.0.113.9"}).status_code
               for i in range(7)]
    assert spoofed == [401] * 6 + [429]
    assert login(proxy, PASSWORD, **{"X-Forwarded-For": "203.0.113.10"}).status_code == 200


def test_trusted_proxies_setting():
    assert parse_trusted_proxies("none") == []
    assert "192.168.0.0/16" in parse_trusted_proxies("private")
    assert parse_trusted_proxies("172.17.0.2, 10.0.0.0/8") == ["172.17.0.2/32", "10.0.0.0/8"]
    with pytest.raises(ValueError):
        parse_trusted_proxies("*")


def test_api_docs_need_login(tmp_path):
    with TestClient(create_app(settings(tmp_path, 31120))) as c:
        assert c.get("/api/docs").status_code == 401
        assert c.get("/api/openapi.json").status_code == 401
        c.post("/api/setup", json={"password": PASSWORD})
        assert c.get("/api/openapi.json").status_code == 200


def test_cross_site_requests_are_refused(tmp_path):
    with TestClient(create_app(settings(tmp_path, 31140))) as c:
        # another site (or a sibling subdomain) must not claim a fresh install
        for site in ("cross-site", "same-site"):
            r = c.post("/api/setup", json={"password": PASSWORD}, headers={"Sec-Fetch-Site": site})
            assert r.status_code == 403
        assert c.get("/api/session").json()["setup_required"] is True
        assert c.post("/api/setup", json={"password": PASSWORD},
                      headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        wa = c.post("/api/wa", json={"name": "x"}).json()
        r = c.delete(f"/api/wa/{wa['id']}", headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403
        assert c.get("/api/wa", headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}).status_code == 403
        # following a link from elsewhere is fine
        assert c.get("/api/status", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}).status_code == 200


def test_cookie_is_secure_behind_https_proxy(app):
    proxy = client_at(app, "172.17.0.2")
    r = login(proxy, PASSWORD, **{"X-Forwarded-For": "203.0.113.30", "X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()
    # direct access (no proxy) and forged headers from an untrusted peer: no Secure flag
    assert "secure" not in login(client_at(app, "192.168.1.50"), PASSWORD).headers["set-cookie"].lower()
    r = login(client_at(app, "198.51.100.8"), PASSWORD, **{"X-Forwarded-Proto": "https"})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_new_passwords_need_ten_characters(app):
    c = client_at(app, "192.168.1.60")
    assert login(c, PASSWORD).status_code == 200
    r = c.post("/api/settings/password", json={"current": PASSWORD, "new": "short1234"})
    assert r.status_code == 400 and "10 characters" in r.json()["detail"]
    assert c.post("/api/settings/password", json={"current": PASSWORD, "new": "much longer pw"}).status_code == 200


def test_bearer_token(app):
    c = client_at(app, "192.168.1.61")
    assert login(c, PASSWORD).status_code == 200
    token = c.get("/api/settings").json()["api_token"]
    bot = client_at(app, "192.168.1.62")
    assert bot.get("/api/status").status_code == 401
    assert bot.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert bot.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
