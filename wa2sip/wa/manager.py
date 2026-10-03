"""Runs one WaRuntime per configured WhatsApp account."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from ..models import WaAccount
from ..settings import Settings
from .base import WaCall, WaRuntime
from .system import System

log = logging.getLogger("wa2sip.wa")


async def sandbox_supported() -> bool:
    """Chromium's sandbox needs unprivileged user + pid + net namespaces."""
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            unshare, "--user", "--map-root-user", "--pid", "--net", "--fork", "true",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(proc.wait(), 5) == 0
    except (OSError, TimeoutError):
        return False


class WaManager:
    def __init__(self, settings: Settings, on_call: Callable[[WaCall], None]):
        self.settings = settings
        self.on_call = on_call
        self.runtimes: dict[str, WaRuntime] = {}
        self.system: System | None = None
        self.sandbox = False
        self._lock = asyncio.Lock()

    @property
    def driver(self) -> str:
        return self.settings.wa_driver

    async def start(self) -> None:
        if self.driver != "chromium":
            log.info("WhatsApp driver: %s (simulated accounts)", self.driver)
            return
        mode = self.settings.chromium_sandbox
        if mode in ("1", "true", "yes", "on"):
            self.sandbox = True
        elif mode in ("0", "false", "no", "off"):
            self.sandbox = False
        else:
            self.sandbox = await sandbox_supported()
        if not self.sandbox:
            log.warning("Chromium runs without its sandbox (this container can't create user namespaces). "
                        "See docs/security.md to enable it.")
        self.system = System(self.settings.runtime_dir, self.settings.display, self.settings.use_xvfb)
        await self.system.start()

    async def stop(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.runtimes.values()), return_exceptions=True)
        self.runtimes.clear()
        if self.system:
            await self.system.stop()

    def _make(self, acc: WaAccount) -> WaRuntime:
        if self.driver == "fake":
            from .fake import FakeRuntime
            rt: WaRuntime = FakeRuntime(acc.id, acc.name)
        else:
            from .agent import AgentRuntime
            assert self.system
            rt = AgentRuntime(acc.id, acc.name, self.settings, self.system, self.sandbox)
        rt.on_call = self.on_call
        return rt

    async def set_accounts(self, accounts: list[WaAccount]) -> None:
        async with self._lock:
            wanted = {a.id: a for a in accounts if a.enabled}
            for aid in list(self.runtimes):
                if aid not in wanted:
                    rt = self.runtimes.pop(aid)
                    log.info("stopping WhatsApp account %s", rt.name)
                    await rt.stop()
            for aid, acc in wanted.items():
                rt = self.runtimes.get(aid)
                if rt:
                    rt.name = acc.name
                    continue
                rt = self._make(acc)
                self.runtimes[aid] = rt
                log.info("starting WhatsApp account %s", acc.name)
                await rt.start()

    async def forget(self, account_id: str) -> None:
        """An account was deleted: stop it, drop its devices and its browser profile."""
        rt = self.runtimes.pop(account_id, None)
        if rt:
            remove = getattr(rt, "remove", None)
            await (remove() if remove else rt.stop())
        profile = Path(self.settings.data_dir) / "wa" / account_id
        if profile.exists():
            await asyncio.to_thread(shutil.rmtree, profile, True)

    def get(self, account_id: str) -> WaRuntime | None:
        return self.runtimes.get(account_id)

    def status(self) -> dict:
        return {"driver": self.driver, "sandbox": self.sandbox,
                "system": self.system.info() if self.system else None,
                "accounts": {aid: rt.status() for aid, rt in self.runtimes.items()}}
