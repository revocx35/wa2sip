"""Integration API v1 for telerouter (and similar callers).

  GET  /api/integration/v1/info      app, version, SIP port
  GET  /api/integration/v1/targets   WhatsApp contacts that can be called
  POST /api/integration/v1/calls     {"target": id} | {"number": "...", "account": id}
                                     -> {"sip_uri": "sip:<one-time user>@<ip>:<port>", "expires_in": 30}
  PUT  /api/integration/v1/inbound   {"sip_uri": "sip:...@host:port", "enabled": bool}
                                     ring this address for incoming WhatsApp calls (see Engine.on_wa_call)

Auth: `Authorization: Bearer <integration token>`. The token is made in Settings -> Integrations, shown
once, and only its SHA-256 is stored. It reaches nothing but these endpoints.

A call works like this: the caller asks for a ticket (POST /calls), then sends a SIP INVITE to the
returned one-time address from the same IP within 30 s. wa2sip dials WhatsApp, answers 180 while it
rings, 200 when the contact picks up (or 486/404/480/603 when not), and bridges the audio.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .models import digits_only

if TYPE_CHECKING:
    from .engine import Engine
    from .store import Store

log = logging.getLogger("wa2sip.integration")

TICKET_TTL = 30.0
MAX_TICKETS = 20
URI_RE = re.compile(r"^sip:[A-Za-z0-9._~-]{8,128}@[A-Za-z0-9.\-]+(:\d{1,5})?$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "w2s_" + secrets.token_urlsafe(32)


@dataclass
class Ticket:
    user: str
    account_id: str
    target: str                  # what to dial on WhatsApp (wa id or number)
    label: str
    source_ip: str
    expires: float = field(default_factory=lambda: time.monotonic() + TICKET_TTL)


class Tickets:
    """One-time SIP addresses for direct calls (single use, short-lived, bound to the caller's IP)."""

    def __init__(self) -> None:
        self._t: dict[str, Ticket] = {}

    def create(self, account_id: str, target: str, label: str, source_ip: str) -> Ticket:
        now = time.monotonic()
        self._t = {k: v for k, v in self._t.items() if v.expires > now}
        if len(self._t) >= MAX_TICKETS:
            raise HTTPException(429, "too many pending calls")
        user = "tr" + secrets.token_hex(16)
        t = Ticket(user, account_id, target, label, source_ip)
        self._t[user] = t
        return t

    def take(self, user: str, source_ip: str) -> Ticket | None:
        t = self._t.get(user)
        if not t or t.expires < time.monotonic():
            return None
        if t.source_ip and source_ip and t.source_ip != source_ip:
            log.warning("integration call ticket used from %s instead of %s - refused", source_ip, t.source_ip)
            return None
        del self._t[user]
        return t

    def peek(self, user: str) -> Ticket | None:
        t = self._t.get(user)
        return t if t and t.expires >= time.monotonic() else None


class CallBody(BaseModel):
    target: str = Field(default="", max_length=128)
    number: str = Field(default="", max_length=32)
    account: str = Field(default="", max_length=32)


class InboundBody(BaseModel):
    sip_uri: str = Field(default="", max_length=200)
    enabled: bool = False


def target_id(account_id: str, wa_id: str) -> str:
    return f"{account_id}:{wa_id}"


def router(engine: "Engine", store: "Store") -> APIRouter:
    r = APIRouter(prefix="/api/integration/v1")

    def check(request: Request) -> str:
        if request.url.scheme != "https":
            raise HTTPException(403, "the integration API is served over HTTPS only (WA2SIP_HTTPS_PORT)")
        s = store.config.settings
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not s.integration_token_hash or not tok or \
                not hmac.compare_digest(token_hash(tok), s.integration_token_hash):
            raise HTTPException(401, "invalid integration token")
        return request.client.host if request.client else ""

    async def account_contacts(aid: str) -> list[dict]:
        rt = engine.wa.get(aid)
        if not rt or not rt.ready:
            return []
        try:
            return await rt.contacts()
        except Exception as e:
            log.debug("contacts of %s: %s", aid, e)
            return []

    async def targets() -> list[dict]:
        out: dict[str, dict] = {}
        cfg = store.config
        for acc in cfg.wa_accounts:
            if not acc.enabled:
                continue
            for c in await account_contacts(acc.id):
                if not (c.get("isMyContact") or c.get("name")):
                    continue
                wid = c.get("id") or ""
                if not wid:
                    continue
                name = c.get("name") or c.get("pushname") or (f"+{c['number']}" if c.get("number") else wid)
                out[target_id(acc.id, wid)] = {"id": target_id(acc.id, wid), "name": name, "kind": "whatsapp",
                                               "number": c.get("number") or "", "account": acc.id}
            for b in cfg.bridges:                       # named bridge contacts win (the owner chose them)
                if b.wa_account_id != acc.id or not b.enabled:
                    continue
                for c in b.contacts:
                    wid = c.wa_id or (f"{c.number}@c.us" if c.number else "")
                    if not wid:
                        continue
                    tid = target_id(acc.id, wid)
                    prev = out.get(tid, {})
                    out[tid] = {"id": tid, "name": c.name or prev.get("name") or c.label(), "kind": "whatsapp",
                                "number": c.number or prev.get("number", ""), "account": acc.id}
        return sorted(out.values(), key=lambda t: t["name"].lower())

    @r.get("/info")
    async def info(request: Request):
        check(request)
        return {"app": "wa2sip", "version": engine.settings.version, "sip_port": engine.settings.sip_port,
                "accounts": [{"id": a.id, "name": a.name, "ready": bool(engine.wa.get(a.id) and engine.wa.get(a.id).ready)}
                             for a in store.config.wa_accounts if a.enabled]}

    @r.get("/targets")
    async def list_targets(request: Request):
        check(request)
        return {"targets": await targets()}

    @r.post("/calls")
    async def call(body: CallBody, request: Request):
        ip = check(request)
        cfg = store.config
        if body.target:
            aid, _, wid = body.target.partition(":")
            if not aid or not wid:
                raise HTTPException(422, "unknown target")
            label = next((t["name"] for t in await targets() if t["id"] == body.target), wid.split("@")[0])
            dial = wid
        else:
            n = digits_only(body.number)
            if len(n) < 6:
                raise HTTPException(422, "number or target required")
            aid = body.account or next((a.id for a in cfg.wa_accounts if a.enabled), "")
            dial, label = n, f"+{n}"
        acc = store.wa_account(aid)
        rt = engine.wa.get(aid)
        if not acc or not acc.enabled or not rt:
            raise HTTPException(404, "unknown WhatsApp account")
        if not rt.ready:
            raise HTTPException(503, "WhatsApp is not connected")
        t = engine.tickets.create(aid, dial, label, ip)
        host = engine.settings.advertise_ip or _local_ip_for(ip)
        log.info("integration call ticket for %s (from %s)", label, ip)
        return {"sip_uri": f"sip:{t.user}@{host}:{engine.settings.sip_port}", "expires_in": int(TICKET_TTL)}

    @r.put("/inbound")
    async def inbound(body: InboundBody, request: Request):
        ip = check(request)
        s = store.config.settings
        uri = body.sip_uri.strip()
        if body.enabled and not URI_RE.match(uri):
            raise HTTPException(422, "sip_uri must be sip:<user>@<host>[:port]")
        changed = (s.telerouter_uri, s.telerouter_enabled) != (uri if body.enabled else s.telerouter_uri, body.enabled)
        if body.enabled:
            s.telerouter_uri = uri
        s.telerouter_enabled = body.enabled
        s.telerouter_peer = ip
        if changed:
            store.save()
            log.info("incoming WhatsApp calls %s telerouter (%s)", "ring" if body.enabled else "no longer ring", ip)
        return {"ok": True}

    return r


def _local_ip_for(peer: str) -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer or "192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
