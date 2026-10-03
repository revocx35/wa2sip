"""JSON file persistence for the configuration and call history, plus routing lookups."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from .models import Bridge, BridgeContact, Config, digits_only

log = logging.getLogger("wa2sip.store")

HISTORY_LIMIT = 300


def peer_keys(peer: dict) -> tuple[set[str], str]:
    """Ids and phone number a WhatsApp peer can be matched by."""
    ids = {x for x in (peer.get("jid"), peer.get("lid"), peer.get("pn_jid")) if x}
    number = digits_only(peer.get("number") or "")
    if number:
        ids.add(f"{number}@c.us")
    return ids, number


def contact_matches(c: BridgeContact, ids: set[str], number: str) -> bool:
    if c.wa_id and c.wa_id in ids:
        return True
    return bool(c.number and number and c.number == number)


class Store:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.path = self.dir / "config.json"
        self.history_path = self.dir / "call_history.json"
        self._lock = threading.Lock()
        self.config = self._load()
        self.history: list[dict] = self._load_history()

    def _load(self) -> Config:
        if self.path.exists():
            try:
                return Config.model_validate_json(self.path.read_text())
            except Exception as e:
                backup = self.path.with_suffix(".json.broken")
                self.path.replace(backup)
                log.error("config.json unreadable (%s) - moved to %s, starting fresh", e, backup)
        cfg = Config()
        self._write(self.path, cfg.model_dump_json(indent=2))
        return cfg

    def _load_history(self) -> list[dict]:
        try:
            return json.loads(self.history_path.read_text())[-HISTORY_LIMIT:]
        except Exception:
            return []

    def _write(self, path: Path, text: str) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except Exception:
                os.unlink(tmp)
                raise

    def save(self) -> None:
        self._write(self.path, self.config.model_dump_json(indent=2))

    def add_history(self, entry: dict) -> None:
        self.history.append(entry)
        self.history = self.history[-HISTORY_LIMIT:]
        self._write(self.history_path, json.dumps(self.history))

    def clear_history(self) -> None:
        self.history = []
        self._write(self.history_path, "[]")

    # -- lookups -------------------------------------------------------------------------
    def pbx(self, pid: str):
        return next((p for p in self.config.pbxs if p.id == pid), None)

    def extension(self, eid: str):
        return next((e for e in self.config.extensions if e.id == eid), None)

    def wa_account(self, aid: str):
        return next((a for a in self.config.wa_accounts if a.id == aid), None)

    def bridge(self, bid: str):
        return next((b for b in self.config.bridges if b.id == bid), None)

    def bridge_for_extension(self, eid: str) -> Bridge | None:
        return next((b for b in self.config.bridges if b.enabled and b.extension_id == eid), None)

    # -- routing --------------------------------------------------------------------------
    def route_wa_call(self, account_id: str, peer: dict) -> tuple[Bridge | None, BridgeContact | None, str]:
        """Bridge (and contact entry) for an incoming WhatsApp call from `peer`.

        A bridge that lists the caller wins; otherwise the account's catch-all bridge
        (all_contacts) takes the call. Returns (bridge, contact, reason).
        """
        ids, number = peer_keys(peer)
        bridges = [b for b in self.config.bridges
                   if b.enabled and b.inbound_enabled and b.wa_account_id == account_id]
        if not bridges:
            return None, None, "no bridge for this WhatsApp account"
        for b in bridges:
            for c in b.contacts:
                if c.inbound and contact_matches(c, ids, number):
                    return b, c, "contact is listed"
        for b in bridges:
            if b.all_contacts:
                return b, None, "catch-all bridge"
        return None, None, "caller is not bridged"

    def routing_conflict(self, bridge: Bridge) -> str | None:
        """Why `bridge` can't coexist with the other enabled bridges (None if it can)."""
        if not bridge.enabled:
            return None
        for other in self.config.bridges:
            if other.id == bridge.id or not other.enabled:
                continue
            name = other.name or other.id
            if other.extension_id == bridge.extension_id:
                return f"this extension is already used by bridge '{name}'"
            if other.wa_account_id != bridge.wa_account_id:
                continue
            if not (bridge.inbound_enabled and other.inbound_enabled):
                continue
            if bridge.all_contacts and other.all_contacts:
                return (f"bridge '{name}' already takes all WhatsApp calls of this account; "
                        "only one catch-all bridge per account")
            for c in bridge.contacts:
                if not c.inbound:
                    continue
                ids = {c.wa_id} if c.wa_id else set()
                for oc in other.contacts:
                    if oc.inbound and contact_matches(oc, ids | ({f"{c.number}@c.us"} if c.number else set()),
                                                      c.number):
                        return f"{c.label()} already rings through bridge '{name}'"
        codes = [code for code, _ in bridge.menu_codes()]
        if bridge.dial_number:
            codes.append(bridge.dial_digit)
        if len(codes) != len(set(codes)):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            return f"menu key {', '.join(dup)} is used twice"
        return None
