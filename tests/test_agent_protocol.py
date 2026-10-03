"""AgentRuntime <-> agent process protocol, with a Python stand-in for the Node agent."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from wa2sip.settings import Settings
from wa2sip.wa.agent import AgentRuntime
from wa2sip.wa.base import WaError


class FakeSystem:
    server = "unix:/nonexistent"

    async def ensure_devices(self, account_id):
        return {"speaker": f"wa_{account_id}_spk", "mic_sink": f"wa_{account_id}_mic", "mic": f"wa_{account_id}_in"}

    async def remove_devices(self, account_id):
        pass

    def env(self):
        return dict(os.environ)


async def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    shutil.copy(Path(__file__).parent / "fake_agent.py", agent_dir / "index.js")
    s = Settings(data_dir=str(tmp_path), node_bin=sys.executable, agent_dir=str(agent_dir))
    rt = AgentRuntime("a1", "Test", s, FakeSystem(), sandbox=False)
    return rt


@pytest.mark.asyncio
async def test_agent_lifecycle_and_calls(runtime):
    events = []
    runtime.on_call = events.append
    await runtime.start()
    try:
        assert await wait_for(lambda: runtime.state == "ready")
        assert runtime.me == {"id": "490000@c.us", "number": "490000"}
        assert runtime.qr is None                      # cleared once linked
        assert await runtime.contacts() == [{"id": "4911@c.us", "number": "4911", "name": "A"}]
        call = await runtime.dial("4911")
        assert call.id == "C1" and call.outgoing
        assert await wait_for(lambda: runtime.calls.get("C1") and runtime.calls["C1"].state == "active")
        await runtime.hangup("C1")
        assert await wait_for(lambda: "C1" not in runtime.calls)
        assert [e.state for e in events] == ["calling", "active", "ended"]
        assert events[-1].reason == "hung up"
        with pytest.raises(WaError) as e:
            await runtime.dial("4919999")
        assert e.value.code == "not_on_whatsapp"
        with pytest.raises(WaError) as e:
            await runtime._rpc("slow", timeout=0.2)
        assert e.value.code == "timeout"
        with pytest.raises(WaError) as e:
            await runtime._rpc("bogus")
        assert e.value.code == "unknown_command"
    finally:
        await runtime.stop()
    assert runtime.state == "stopped"


@pytest.mark.asyncio
async def test_agent_is_restarted_after_a_crash(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_CRASH", str(tmp_path / "crashed-once"))
    monkeypatch.setattr("wa2sip.wa.agent.asyncio.sleep", _fast_sleep)
    await runtime.start()
    try:
        assert await wait_for(lambda: runtime.state == "ready", 10)
        assert runtime.restarts == 1
    finally:
        await runtime.stop()


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **kw):
    await _real_sleep(min(delay, 0.05), *a, **kw)


@pytest.mark.asyncio
async def test_operations_need_a_ready_account(runtime):
    with pytest.raises(WaError) as e:
        await runtime.dial("4911")
    assert e.value.code == "not_ready"
