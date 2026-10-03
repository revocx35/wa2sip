"""FastAPI application: REST API + static single-page web UI."""

from __future__ import annotations

import asyncio
import hmac
import io
import logging
import math
import secrets
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ..engine import Engine
from ..logbuffer import LogBuffer
from ..media import piper as piper_mod
from ..media import tts as tts_mod
from ..models import SECRET_FIELDS, Bridge, Extension, Pbx, WaAccount, redact
from ..settings import Settings
from ..store import Store
from ..wa.base import WaError
from . import auth

log = logging.getLogger("wa2sip.web")
STATIC = Path(__file__).parent / "static"
PUBLIC = {"/api/health", "/api/session", "/api/login", "/api/logout", "/api/setup"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CONTACTS_TTL = 300


def cross_site(headers, method: str = "POST") -> bool:
    """True for a browser request that another site started (Fetch Metadata).

    SameSite=strict keeps the cookie off cross-site requests, but not off requests from sibling
    subdomains (same site), and it doesn't cover /api/setup, which needs no cookie. Only our own
    pages may change state; plain navigations are fine. Clients that don't send Sec-Fetch-Site
    (curl, automations) aren't browsers and are unaffected.
    """
    site = headers.get("sec-fetch-site")
    if site is None or site in ("same-origin", "none"):
        return False
    return not (method in SAFE_METHODS and headers.get("sec-fetch-mode") == "navigate")


class PasswordBody(BaseModel):
    password: str = Field(max_length=1024)


class ChangePasswordBody(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


class PairingBody(BaseModel):
    phone: str = Field(max_length=32)


class SimulateBody(BaseModel):
    number: str = ""
    name: str = ""


class PreviewBody(BaseModel):
    text: str = Field(default="", max_length=2000)
    voice: str = ""
    speed: int = 0
    bridge: dict | None = None      # a bridge form: preview its whole menu


class SettingsBody(BaseModel):
    default_voice: str | None = None
    default_speed: int | None = None
    ringback_style: str | None = None


def wav_bytes(samples) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def setup_logging(settings: Settings, buffer: LogBuffer) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, LogBuffer) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    buffer.setLevel(logging.INFO)
    root.addHandler(buffer)
    logging.getLogger("wa2sip").setLevel(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    if settings.sip_trace:
        logging.getLogger("wa2sip.sip.trace").setLevel(logging.DEBUG)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logbuf = LogBuffer()
    setup_logging(settings, logbuf)
    store = Store(settings.data_dir)
    engine = Engine(settings, store)
    cfg = store.config
    contacts_cache: dict[str, tuple[float, list[dict]]] = {}

    if settings.admin_password and not cfg.settings.admin_password_hash:
        if len(settings.admin_password) < auth.MIN_PASSWORD_LENGTH:
            log.warning("ADMIN_PASSWORD is shorter than %d characters - choose a longer one before "
                        "exposing the UI", auth.MIN_PASSWORD_LENGTH)
        cfg.settings.admin_password_hash = auth.hash_password(settings.admin_password)
        store.save()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("wa2sip %s starting (web :%d, sip udp :%d, rtp %d-%d, whatsapp driver %s)", settings.version,
                 settings.web_port, settings.sip_port, settings.rtp_port_min, settings.rtp_port_max,
                 settings.wa_driver)
        await engine.start()
        yield
        await engine.stop()

    app = FastAPI(title="wa2sip", version=settings.version, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.engine = engine
    app.state.store = store
    app.state.logs = logbuf

    # -- auth -------------------------------------------------------------------------------
    def authed(request: Request) -> bool:
        s = store.config.settings
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and s.api_token:
            return hmac.compare_digest(header[7:].strip(), s.api_token)
        return auth.check_session(request.cookies.get(auth.COOKIE), s.session_secret, s.admin_password_hash)

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            if cross_site(request.headers, request.method):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            if path not in PUBLIC and not authed(request):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # X-Forwarded-For/-Proto count only from trusted reverse proxies (WA2SIP_TRUSTED_PROXIES).
    # Added last, so it runs first; uvicorn's own proxy handling is off (see __main__).
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

    throttle = auth.LoginThrottle()
    scrypt_slots = asyncio.Semaphore(2)

    async def password_ok(password: str, stored: str) -> bool:
        # scrypt takes ~50 ms of CPU: run it off the event loop so calls keep their audio
        async with scrypt_slots:
            return await asyncio.to_thread(auth.verify_password, password, stored)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def charge_attempt(ip: str) -> None:
        try:
            throttle.begin(ip)
        except auth.Locked as e:
            wait = max(1, math.ceil(e.retry_after))
            hint = f"{wait} s" if wait < 120 else f"{math.ceil(wait / 60)} min"
            raise HTTPException(429, f"too many wrong passwords - try again in {hint}",
                                headers={"Retry-After": str(wait)}) from None

    def cookie_secure(request: Request) -> bool:
        if settings.secure_cookies == "auto":
            # HTTPS as reported by a trusted reverse proxy
            return request.url.scheme == "https" and "x-forwarded-proto" in request.headers
        return settings.secure_cookies == "true"

    def set_cookie(request: Request, resp: Response) -> None:
        s = store.config.settings
        resp.set_cookie(auth.COOKIE, auth.make_session(s.session_secret, s.admin_password_hash),
                        max_age=auth.SESSION_TTL, httponly=True, samesite="strict",
                        secure=cookie_secure(request))

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": settings.version}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request),
                "setup_required": not store.config.settings.admin_password_hash,
                "version": settings.version, "driver": settings.wa_driver}

    @app.post("/api/setup")
    async def setup(body: PasswordBody, request: Request, response: Response):
        if store.config.settings.admin_password_hash:
            raise HTTPException(409, "already set up")
        if len(body.password) < auth.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
        pw_hash = await asyncio.to_thread(auth.hash_password, body.password)
        if store.config.settings.admin_password_hash:     # a parallel setup won the race
            raise HTTPException(409, "already set up")
        store.config.settings.admin_password_hash = pw_hash
        store.save()
        log.info("admin password set from %s", client_ip(request))
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: PasswordBody, request: Request, response: Response):
        h = store.config.settings.admin_password_hash
        if not h:
            raise HTTPException(401, "wrong password")
        ip = client_ip(request)
        charge_attempt(ip)
        if not await password_ok(body.password, h):
            log.warning("failed admin login from %s", ip)
            raise HTTPException(401, "wrong password")
        throttle.success(ip)
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        response.delete_cookie(auth.COOKIE, httponly=True, samesite="strict", secure=cookie_secure(request))
        return {"ok": True}

    # -- helpers ----------------------------------------------------------------------------
    def merge(model_cls, existing, payload: dict):
        data = existing.model_dump() if existing else {}
        for k, v in payload.items():
            if k in ("id", "contact_user") and existing:
                continue
            if k in SECRET_FIELDS and (v is None or v == "") and existing:
                continue
            if k.endswith("_set"):
                continue
            data[k] = v
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(x) for x in first.get("loc", []))
            msg = str(first.get("msg", "")).removeprefix("Value error, ")
            raise HTTPException(422, f"{field}: {msg}" if field else msg) from e

    async def save_and_apply():
        store.save()
        await engine.apply_config()

    def find(items, oid):
        item = next((x for x in items if x.id == oid), None)
        if not item:
            raise HTTPException(404, "not found")
        return item

    def replace(items, item):
        for i, x in enumerate(items):
            if x.id == item.id:
                items[i] = item
                return
        items.append(item)

    def runtime(aid: str):
        find(cfg.wa_accounts, aid)
        rt = engine.wa.get(aid)
        if not rt:
            raise HTTPException(409, "this WhatsApp account is disabled")
        return rt

    def wa_http_error(e: WaError) -> HTTPException:
        status = {"not_ready": 409, "not_running": 409, "busy": 409, "timeout": 504,
                  "not_on_whatsapp": 404, "no_such_call": 404}.get(e.code, 502)
        return HTTPException(status, str(e))

    # -- status & logs ------------------------------------------------------------------------
    @app.get("/api/status")
    async def status():
        st = engine.status()
        st["counts"] = {"pbxs": len(cfg.pbxs), "extensions": len(cfg.extensions),
                        "wa_accounts": len(cfg.wa_accounts), "bridges": len(cfg.bridges)}
        st["version"] = settings.version
        return st

    @app.get("/api/logs")
    async def logs(after: int = 0):
        return {"seq": logbuf.seq, "records": logbuf.since(after)}

    # -- PBX servers ----------------------------------------------------------------------------
    @app.get("/api/pbxs")
    async def list_pbxs():
        return [p.model_dump() for p in cfg.pbxs]

    @app.post("/api/pbxs")
    async def create_pbx(payload: dict):
        p = merge(Pbx, None, payload)
        cfg.pbxs.append(p)
        await save_and_apply()
        return p.model_dump()

    @app.put("/api/pbxs/{pid}")
    async def update_pbx(pid: str, payload: dict):
        p = merge(Pbx, find(cfg.pbxs, pid), payload)
        replace(cfg.pbxs, p)
        await save_and_apply()
        return p.model_dump()

    @app.delete("/api/pbxs/{pid}")
    async def delete_pbx(pid: str):
        find(cfg.pbxs, pid)
        used = [e.username for e in cfg.extensions if e.pbx_id == pid]
        if used:
            raise HTTPException(409, f"delete its extensions first ({', '.join(used)})")
        cfg.pbxs = [p for p in cfg.pbxs if p.id != pid]
        await save_and_apply()
        return {"ok": True}

    # -- extensions -------------------------------------------------------------------------------
    @app.get("/api/extensions")
    async def list_extensions():
        return [redact(e) for e in cfg.extensions]

    def check_extension(e: Extension) -> None:
        if not store.pbx(e.pbx_id):
            raise HTTPException(422, "pbx_id: unknown PBX")
        for other in cfg.extensions:
            if other.id != e.id and other.pbx_id == e.pbx_id and other.username == e.username:
                raise HTTPException(409, f"extension {e.username} is already set up on this PBX")

    @app.post("/api/extensions")
    async def create_extension(payload: dict):
        e = merge(Extension, None, payload)
        check_extension(e)
        cfg.extensions.append(e)
        await save_and_apply()
        return redact(e)

    @app.put("/api/extensions/{eid}")
    async def update_extension(eid: str, payload: dict):
        e = merge(Extension, find(cfg.extensions, eid), payload)
        check_extension(e)
        replace(cfg.extensions, e)
        await save_and_apply()
        return redact(e)

    @app.delete("/api/extensions/{eid}")
    async def delete_extension(eid: str):
        find(cfg.extensions, eid)
        used = [b.name or b.id for b in cfg.bridges if b.extension_id == eid]
        if used:
            raise HTTPException(409, f"used by bridge {', '.join(used)}")
        cfg.extensions = [e for e in cfg.extensions if e.id != eid]
        await save_and_apply()
        return {"ok": True}

    @app.post("/api/extensions/{eid}/register")
    async def reregister(eid: str):
        find(cfg.extensions, eid)
        acc = engine.ua.accounts.get(eid)
        if not acc:
            raise HTTPException(409, "extension or its PBX is disabled")
        acc.refresh_now()
        return {"ok": True}

    # -- WhatsApp accounts ------------------------------------------------------------------------
    @app.get("/api/wa")
    async def list_wa():
        out = []
        for a in cfg.wa_accounts:
            rt = engine.wa.get(a.id)
            out.append({**a.model_dump(), "status": rt.status() if rt else {"state": "disabled"}})
        return out

    @app.post("/api/wa")
    async def create_wa(payload: dict):
        a = merge(WaAccount, None, payload)
        cfg.wa_accounts.append(a)
        await save_and_apply()
        return a.model_dump()

    @app.put("/api/wa/{aid}")
    async def update_wa(aid: str, payload: dict):
        a = merge(WaAccount, find(cfg.wa_accounts, aid), payload)
        replace(cfg.wa_accounts, a)
        await save_and_apply()
        return a.model_dump()

    @app.delete("/api/wa/{aid}")
    async def delete_wa(aid: str):
        find(cfg.wa_accounts, aid)
        used = [b.name or b.id for b in cfg.bridges if b.wa_account_id == aid]
        if used:
            raise HTTPException(409, f"used by bridge {', '.join(used)}")
        rt = engine.wa.get(aid)
        if rt and rt.ready:
            try:
                await rt.logout()
            except WaError as e:
                log.warning("logout before delete failed: %s", e)
        cfg.wa_accounts = [a for a in cfg.wa_accounts if a.id != aid]
        store.save()
        await engine.wa.forget(aid)
        await engine.apply_config()
        contacts_cache.pop(aid, None)
        return {"ok": True}

    @app.get("/api/wa/{aid}/status")
    async def wa_status(aid: str):
        return runtime(aid).status()

    @app.post("/api/wa/{aid}/restart")
    async def wa_restart(aid: str):
        rt = runtime(aid)
        asyncio.create_task(rt.restart())
        return {"ok": True}

    @app.post("/api/wa/{aid}/logout")
    async def wa_logout(aid: str):
        rt = runtime(aid)
        try:
            await rt.logout()
        except WaError as e:
            raise wa_http_error(e) from e
        contacts_cache.pop(aid, None)
        return {"ok": True}

    @app.post("/api/wa/{aid}/pairing-code")
    async def wa_pairing(aid: str, body: PairingBody):
        rt = runtime(aid)
        phone = "".join(ch for ch in body.phone if ch.isdigit())
        if len(phone) < 8:
            raise HTTPException(422, "enter the phone number with country code")
        try:
            code = await rt.request_pairing_code(phone)
        except WaError as e:
            raise wa_http_error(e) from e
        return {"code": code}

    @app.get("/api/wa/{aid}/contacts")
    async def wa_contacts(aid: str, refresh: bool = False):
        rt = runtime(aid)
        hit = contacts_cache.get(aid)
        if hit and not refresh and time.time() - hit[0] < CONTACTS_TTL:
            return hit[1]
        try:
            items = await rt.contacts()
        except WaError as e:
            if hit:
                return hit[1]
            raise wa_http_error(e) from e
        items.sort(key=lambda c: ((c.get("name") or c.get("pushname") or "~").lower(), c.get("number") or ""))
        contacts_cache[aid] = (time.time(), items)
        return items

    @app.get("/api/wa/{aid}/lookup")
    async def wa_lookup(aid: str, number: str):
        rt = runtime(aid)
        try:
            found = await rt.lookup(number)
        except WaError as e:
            raise wa_http_error(e) from e
        if not found:
            raise HTTPException(404, "this number is not on WhatsApp")
        return found

    @app.get("/api/wa/{aid}/screenshot.jpg")
    async def wa_screenshot(aid: str):
        rt = runtime(aid)
        try:
            data = await rt.screenshot()
        except WaError as e:
            raise wa_http_error(e) from e
        if not data:
            raise HTTPException(404, "no browser view for this account")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/wa/{aid}/diagnostics")
    async def wa_diagnostics(aid: str):
        return await runtime(aid).diagnostics()

    @app.post("/api/wa/{aid}/simulate-call")
    async def wa_simulate(aid: str, body: SimulateBody):
        rt = runtime(aid)
        sim = getattr(rt, "simulate_incoming", None)
        if not sim:
            raise HTTPException(409, "only simulated accounts (WA2SIP_WA_DRIVER=fake) can fake calls")
        try:
            call = sim(body.number, body.name)
        except WaError as e:
            raise wa_http_error(e) from e
        return call.info()

    @app.post("/api/wa/{aid}/simulate-hangup")
    async def wa_simulate_hangup(aid: str):
        rt = runtime(aid)
        sim = getattr(rt, "simulate_peer_hangup", None)
        if not sim:
            raise HTTPException(409, "only simulated accounts can fake calls")
        sim()
        return {"ok": True}

    # -- bridges ---------------------------------------------------------------------------------------
    def check_bridge(b: Bridge) -> None:
        if not store.wa_account(b.wa_account_id):
            raise HTTPException(422, "wa_account_id: unknown WhatsApp account")
        if not store.extension(b.extension_id):
            raise HTTPException(422, "extension_id: unknown extension")
        needs_ring = b.all_contacts or any(c.inbound and not c.ring for c in b.contacts)
        if b.enabled and b.inbound_enabled and needs_ring and not b.ring_targets:
            raise HTTPException(422, "ring_targets: enter the extension(s) to ring for incoming WhatsApp calls")
        conflict = store.routing_conflict(b)
        if conflict:
            raise HTTPException(409, conflict)

    def bridge_out(b: Bridge) -> dict:
        d = b.model_dump()
        d["menu"] = [{"code": code, "name": c.label()} for code, c in b.menu_codes()]
        d["menu_text"] = engine.menu_text(b)
        return d

    @app.get("/api/bridges")
    async def list_bridges():
        return [bridge_out(b) for b in cfg.bridges]

    @app.post("/api/bridges")
    async def create_bridge(payload: dict):
        b = merge(Bridge, None, payload)
        check_bridge(b)
        cfg.bridges.append(b)
        await save_and_apply()
        return bridge_out(b)

    @app.put("/api/bridges/{bid}")
    async def update_bridge(bid: str, payload: dict):
        b = merge(Bridge, find(cfg.bridges, bid), payload)
        check_bridge(b)
        replace(cfg.bridges, b)
        await save_and_apply()
        return bridge_out(b)

    @app.delete("/api/bridges/{bid}")
    async def delete_bridge(bid: str):
        find(cfg.bridges, bid)
        cfg.bridges = [b for b in cfg.bridges if b.id != bid]
        await save_and_apply()
        return {"ok": True}

    # -- calls -------------------------------------------------------------------------------------------
    @app.get("/api/calls")
    async def calls():
        return {"active": engine.status()["sessions"], "history": list(reversed(store.history))}

    @app.post("/api/calls/{sid}/hangup")
    async def hangup(sid: str):
        if not await engine.hangup(sid):
            raise HTTPException(404, "no such call")
        return {"ok": True}

    @app.delete("/api/calls/history")
    async def clear_history():
        store.clear_history()
        return {"ok": True}

    # -- text-to-speech -------------------------------------------------------------------------------
    def voice_users(voice: str) -> list[str]:
        users = [f"bridge '{b.name or b.id}'" for b in cfg.bridges if b.voice == voice]
        if cfg.settings.default_voice == voice:
            users.append("default voice")
        return users

    @app.get("/api/tts/voices")
    async def tts_voices():
        """Natural (Piper) voices - installed, downloading and the catalog - plus espeak-ng voices."""
        t = engine.tts
        piper = t.piper
        out = {"espeak": {"available": t.available, "voices": await t.voices()},
               "piper": {"available": bool(piper and piper.available)},
               "default_voice": cfg.settings.default_voice, "default_speed": cfg.settings.default_speed}
        if piper and piper.available:
            installed = piper.installed()
            for v in installed:
                v["used_by"] = voice_users(tts_mod.PIPER_PREFIX + v["key"])
            try:
                catalog = [piper_mod.simplify(k, v) for k, v in (await piper.catalog()).items()]
            except Exception:
                catalog = []
            out["piper"].update(installed=installed, downloads=piper.downloads,
                                catalog=catalog, catalog_error=piper.catalog_error)
        return out

    @app.post("/api/tts/voices/{key}")
    async def download_voice(key: str):
        piper = engine.tts.piper
        if not piper or not piper.available:
            raise HTTPException(409, "Piper is not available in this installation")
        try:
            known = key in await piper.catalog()
        except Exception as e:
            raise HTTPException(502, str(e)) from e
        if not known:
            raise HTTPException(404, "unknown voice")
        piper.start_download(key)
        return {"ok": True, "installed": piper.is_installed(key)}

    @app.delete("/api/tts/voices/{key}")
    async def delete_voice(key: str):
        piper = engine.tts.piper
        if not piper or not piper.is_installed(key):
            raise HTTPException(404, "not installed")
        users = voice_users(tts_mod.PIPER_PREFIX + key)
        if users:
            raise HTTPException(409, f"voice is used by {', '.join(users)}")
        piper.delete(key)
        return {"ok": True}

    @app.post("/api/tts/preview")
    async def tts_preview(body: PreviewBody):
        s = cfg.settings
        if body.bridge is not None:
            payload = dict(body.bridge)
            payload.setdefault("wa_account_id", "preview")
            payload.setdefault("extension_id", "preview")
            b = merge(Bridge, None, payload)
            text = body.text or engine.menu_text(b)
            voice, speed = engine.voice_of(b)
        else:
            text = body.text
            voice, speed = body.voice or s.default_voice, body.speed or s.default_speed
        if not text.strip():
            raise HTTPException(422, "nothing to say")
        samples = await engine.tts.pcm(text, voice, speed)
        return Response(wav_bytes(samples), media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # -- settings --------------------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        s = cfg.settings
        return {
            "version": settings.version,
            "api_token": s.api_token,
            "default_voice": s.default_voice,
            "default_speed": s.default_speed,
            "ringback_style": s.ringback_style,
            "sip_port": settings.sip_port,
            "rtp_ports": f"{settings.rtp_port_min}-{settings.rtp_port_max}",
            "advertise_ip": settings.advertise_ip or "auto",
            "web_port": settings.web_port,
            "wa_driver": settings.wa_driver,
            "chromium_sandbox": engine.wa.sandbox,
            "data_dir": settings.data_dir,
        }

    @app.put("/api/settings")
    async def put_settings(body: SettingsBody):
        s = cfg.settings
        data = s.model_dump()
        for k, v in body.model_dump().items():
            if v is not None:
                data[k] = v
        try:
            new = type(s).model_validate(data)
        except ValidationError as e:
            raise HTTPException(422, str(e.errors()[0].get("msg", "invalid"))) from e
        cfg.settings = new
        await save_and_apply()
        return await get_settings()

    @app.post("/api/settings/password")
    async def change_password(body: ChangePasswordBody, request: Request, response: Response):
        s = cfg.settings
        if len(body.new) < auth.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
        ip = client_ip(request)
        charge_attempt(ip)
        if not await password_ok(body.current, s.admin_password_hash):
            log.warning("wrong current password in password change from %s", ip)
            raise HTTPException(401, "current password is wrong")
        throttle.success(ip)
        s.admin_password_hash = await asyncio.to_thread(auth.hash_password, body.new)
        store.save()
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/settings/api-token")
    async def regenerate_token():
        cfg.settings.api_token = secrets.token_urlsafe(24)
        store.save()
        return {"api_token": cfg.settings.api_token}

    # -- UI ----------------------------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
