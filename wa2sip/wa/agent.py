"""A WhatsApp account driven by real WhatsApp Web in Chromium (the Node agent in /app/agent).

The agent is one Node.js process per account (whatsapp-web.js + puppeteer). It talks JSON
lines on stdin/stdout:

    -> {"id": 7, "cmd": "dial", "args": {"to": "4915112345678"}}
    <- {"id": 7, "ok": true, "result": {...}}      or {"id": 7, "ok": false, "code": "...", "error": "..."}
    <- {"event": "state", "state": "qr", "detail": ""}
    <- {"event": "qr", "qr": "data:image/png;base64,..."}
    <- {"event": "call", "call": {...snapshot of WhatsApp Web's call model...}}
    <- {"event": "log", "level": "info", "msg": "..."}

Call audio does not pass through the agent: Chromium plays into and records from this
account's PulseAudio devices (system.py), and PulsePipe connects those to the SIP call.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from ..settings import Settings
from .base import WA_CALL_STATES, AudioPipe, WaCall, WaError, WaRuntime, end_reason
from .pulse_audio import PulsePipe
from .system import System

log = logging.getLogger("wa2sip.wa")

RPC_TIMEOUT = 45.0


def call_from_snapshot(account_id: str, snap: dict) -> WaCall:
    state = WA_CALL_STATES.get(int(snap.get("state") or 0), "other")
    if snap.get("removed") and state not in ("ended", "elsewhere"):
        state = "ended"
    call = WaCall(id=str(snap.get("id") or ""), account_id=account_id, state=state,
                  outgoing=bool(snap.get("outgoing")), peer=snap.get("peer") or {},
                  is_video=bool(snap.get("isVideo")), is_group=bool(snap.get("isGroup")), raw=snap)
    if call.ended:
        call.reason = end_reason(snap, state)
    return call


class AgentRuntime(WaRuntime):
    def __init__(self, account_id: str, name: str, settings: Settings, system: System, sandbox: bool):
        super().__init__(account_id, name)
        self.settings = settings
        self.system = system
        self.sandbox = sandbox
        self.profile_dir = Path(settings.data_dir) / "wa" / account_id
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task] = []
        self._run_task: asyncio.Task | None = None
        self._stopping = False
        self.devices: dict[str, str] = {}
        self.restarts = 0

    # -- process lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        self._stopping = False
        self.set_state("starting")
        self._run_task = asyncio.create_task(self._run(), name=f"wa-agent-{self.account_id}")

    async def _run(self) -> None:
        backoff = 3.0
        while not self._stopping:
            try:
                await self._spawn()
                assert self.proc
                rc = await self.proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("[%s] agent failed to start", self.name)
                rc = str(e)
            self._fail_pending("agent stopped")
            for call in list(self.calls.values()):
                call.state, call.reason = "ended", "WhatsApp agent stopped"
                self._emit_call(call)
            if self._stopping:
                break
            self.restarts += 1
            self.set_state("failed", f"agent exited ({rc}); restarting")
            log.warning("[%s] WhatsApp agent exited (%s) - restarting in %.0f s", self.name, rc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        self.set_state("stopped")

    async def _spawn(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir, 0o700)
        self.devices = await self.system.ensure_devices(self.account_id)
        env = self.system.env()
        env.update({
            "PULSE_SINK": self.devices["speaker"],
            "PULSE_SOURCE": self.devices["mic"],
            "WA2SIP_ACCOUNT": self.account_id,
            "WA2SIP_PROFILE_DIR": str(self.profile_dir),
            "WA2SIP_CHROMIUM": self.settings.chromium_path,
            "WA2SIP_SANDBOX": "1" if self.sandbox else "0",
            "WA2SIP_HEADLESS": "0" if self.settings.use_xvfb else "1",
            "WA2SIP_RAW_MIC": "1" if self.settings.raw_mic else "0",
            "WA2SIP_TRACE": "1" if self.settings.wa_trace else "0",
        })
        script = Path(self.settings.agent_dir) / "index.js"
        self.proc = await asyncio.create_subprocess_exec(
            self.settings.node_bin, str(script), env=env, cwd=self.settings.agent_dir,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024)
        self.set_state("loading", "starting WhatsApp Web")
        self._tasks = [asyncio.create_task(self._read_stdout(self.proc)),
                       asyncio.create_task(self._read_stderr(self.proc))]
        log.info("[%s] WhatsApp agent started (pid %d)", self.name, self.proc.pid)

    async def stop(self) -> None:
        self._stopping = True
        proc = self.proc
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                await self._rpc("shutdown", timeout=10)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        if self._run_task:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
        for t in self._tasks:
            t.cancel()
        self.set_state("stopped")

    async def remove(self) -> None:
        """Account deleted: stop and drop its audio devices (the profile is removed by the caller)."""
        await self.stop()
        await self.system.remove_devices(self.account_id)

    # -- protocol ---------------------------------------------------------------------------
    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                log.debug("[%s] agent: %s", self.name, line.decode(errors="replace").rstrip())
                continue
            try:
                self._dispatch(msg)
            except Exception:
                log.exception("[%s] agent message handling failed", self.name)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                log.debug("[%s] agent stderr: %s", self.name, text[:500])

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(WaError(msg.get("code") or "error", msg.get("error") or "agent error"))
            return
        ev = msg.get("event")
        if ev == "state":
            state = msg.get("state") or "loading"
            if state != self.state:
                log.info("[%s] WhatsApp: %s%s", self.name, state, f" ({msg['detail']})" if msg.get("detail") else "")
            self.set_state(state, msg.get("detail") or "")
        elif ev == "qr":
            self.qr = msg.get("qr")
            self.set_state("qr")
            self.qr = msg.get("qr")
        elif ev == "pairing_code":
            self.pairing_code = msg.get("code")
        elif ev == "me":
            self.me = msg.get("me")
        elif ev == "diag":
            self.diag = msg.get("diag") or {}
        elif ev == "call":
            self._on_call_snapshot(msg.get("call") or {})
        elif ev == "log":
            level = {"error": logging.ERROR, "warn": logging.WARNING, "info": logging.INFO}.get(
                msg.get("level"), logging.DEBUG)
            log.log(level, "[%s] %s", self.name, msg.get("msg"))

    def _on_call_snapshot(self, snap: dict) -> None:
        call = call_from_snapshot(self.account_id, snap)
        if not call.id:
            return
        prev = self.calls.get(call.id)
        if prev and prev.state == call.state and not call.ended:
            prev.peer = call.peer or prev.peer
            return
        if prev:
            call.created = prev.created
            if not call.peer.get("name") and prev.peer.get("name"):
                call.peer = {**call.peer, "name": prev.peer["name"]}
        elif call.ended:
            return          # never saw it alive: nothing to report
        log.info("[%s] WhatsApp call %s %s %s: %s%s", self.name, call.id[:8],
                 "to" if call.outgoing else "from", call.peer_label(), call.state,
                 f" ({call.reason})" if call.reason else "")
        self._emit_call(call)

    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WaError("agent_stopped", why))
        self._pending.clear()

    async def _rpc(self, cmd: str, timeout: float = RPC_TIMEOUT, **args):
        proc = self.proc
        if not proc or proc.returncode is not None or not proc.stdin:
            raise WaError("not_running", "the WhatsApp browser is not running")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        proc.stdin.write((json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n").encode())
        try:
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise WaError("timeout", f"WhatsApp did not answer '{cmd}' in time") from None
        finally:
            self._pending.pop(rid, None)

    def _need_ready(self) -> None:
        if self.state != "ready":
            raise WaError("not_ready", f"WhatsApp is not connected ({self.state})")

    # -- operations ---------------------------------------------------------------------------
    async def contacts(self) -> list[dict]:
        self._need_ready()
        return await self._rpc("contacts", timeout=60) or []

    async def lookup(self, number: str) -> dict | None:
        self._need_ready()
        return await self._rpc("lookup", number=number)

    async def dial(self, target: str) -> WaCall:
        self._need_ready()
        if self.active_call():
            raise WaError("busy", "WhatsApp is already in a call")
        snap = await self._rpc("dial", to=target, timeout=60)
        call = call_from_snapshot(self.account_id, snap or {})
        if not call.id:
            raise WaError("failed", "WhatsApp did not start the call")
        if call.id not in self.calls and not call.ended:
            self._emit_call(call)
        return self.calls.get(call.id, call)

    async def accept(self, call_id: str) -> None:
        await self._rpc("accept", callId=call_id, timeout=30)

    async def reject(self, call_id: str) -> None:
        await self._rpc("reject", callId=call_id, timeout=15)

    async def hangup(self, call_id: str) -> None:
        await self._rpc("hangup", callId=call_id, timeout=15)

    async def logout(self) -> None:
        await self._rpc("logout", timeout=30)

    async def request_pairing_code(self, phone: str) -> str:
        code = await self._rpc("pairing_code", phone=phone, timeout=60)
        self.pairing_code = code
        return code

    async def screenshot(self) -> bytes | None:
        data = await self._rpc("screenshot", timeout=20)
        return base64.b64decode(data) if data else None

    async def diagnostics(self) -> dict:
        try:
            self.diag = await self._rpc("diagnostics", timeout=20) or {}
        except WaError as e:
            return {**self.diag, "error": str(e)}
        return self.diag

    async def open_audio(self, codec: str, on_audio: Callable[[bytes], None]) -> AudioPipe:
        if not self.devices:
            self.devices = await self.system.ensure_devices(self.account_id)
        pipe = PulsePipe(self.system.server, self.system.env(), self.devices["speaker"],
                         self.devices["mic_sink"], codec, on_audio)
        await pipe.start()
        return pipe

    def status(self) -> dict:
        s = super().status()
        s["restarts"] = self.restarts
        return s
