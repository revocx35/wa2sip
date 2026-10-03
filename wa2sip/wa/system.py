"""Desktop services for the browsers: a private X display (Xvfb) and a PulseAudio server.

Each WhatsApp account gets its own pair of virtual audio devices:

    wa_<id>_spk   null sink: Chromium plays the call into it; we record its monitor (parec)
    wa_<id>_mic   null sink: we play the PBX caller into it (pacat) ...
    wa_<id>_in    ... and this remap of its monitor is Chromium's microphone

PulseAudio converts between G.711 at 8 kHz (us) and 48 kHz float (Chromium) on the fly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("wa2sip.system")

PULSE_CONFIG = """\
load-module module-native-protocol-unix socket={socket} auth-anonymous=1
load-module module-null-sink sink_name=wa2sip_idle sink_properties=device.description=wa2sip-idle
set-default-sink wa2sip_idle
"""
# default.pa is not loaded (-n): no hardware probing, no suspend-on-idle (our null sinks keep a clock)


class Supervised:
    """A child process that is restarted when it exits (until stop())."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str]):
        self.name = name
        self.argv = argv
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.started = asyncio.Event()
        self.restarts = 0

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"supervise-{self.name}")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv, env=self.env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except OSError as e:
                log.error("%s could not start: %s", self.name, e)
                return
            self.started.set()
            assert self.proc.stdout
            async for line in self.proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    log.debug("[%s] %s", self.name, text)
            rc = await self.proc.wait()
            if self._stopping:
                return
            self.restarts += 1
            log.warning("%s exited (code %s) - restarting in %.0f s", self.name, rc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (ProcessLookupError, TimeoutError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        if self._task:
            self._task.cancel()

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)


class System:
    """Xvfb + PulseAudio for the Chromium-driven WhatsApp accounts."""

    def __init__(self, runtime_dir: str, display: str = ":99", use_xvfb: bool = True):
        self.dir = Path(runtime_dir)
        self.display = display
        self.use_xvfb = use_xvfb
        self.pulse_dir = self.dir / "pulse"
        self.socket = self.pulse_dir / "native"
        self.server = f"unix:{self.socket}"
        self.procs: list[Supervised] = []
        self._modules: dict[str, list[int]] = {}       # account id -> loaded module indexes
        self._lock = asyncio.Lock()
        self.error: str | None = None

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "PULSE_SERVER": self.server,
            "PULSE_RUNTIME_PATH": str(self.pulse_dir),
            "XDG_RUNTIME_DIR": str(self.dir),
            "HOME": env.get("HOME") or str(self.dir),
            # no D-Bus in the container: tell PulseAudio and Chromium not to look for one
            "DBUS_SESSION_BUS_ADDRESS": "disabled:",
            "DBUS_SYSTEM_BUS_ADDRESS": "disabled:",
        })
        if self.use_xvfb:
            env["DISPLAY"] = self.display
        return env

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pulse_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        os.chmod(self.pulse_dir, 0o700)
        if self.use_xvfb:
            xvfb = shutil.which("Xvfb")
            if not xvfb:
                self.error = "Xvfb is not installed"
                log.error(self.error)
            else:
                lock = Path(f"/tmp/.X{self.display.lstrip(':')}-lock")
                lock.unlink(missing_ok=True)
                sockets = Path("/tmp/.X11-unix")
                if not sockets.exists():
                    sockets.mkdir(mode=0o1777)
                    os.chmod(sockets, 0o1777)
                # only the socket file in this container's /tmp: no TCP, and no abstract socket,
                # which would be shared with the host when the container uses host networking
                p = Supervised("xvfb", [xvfb, self.display, "-screen", "0", "1280x900x24",
                                        "-nolisten", "tcp", "-nolisten", "local"], self.env())
                p.start()
                self.procs.append(p)
        pulse = shutil.which("pulseaudio")
        if not pulse:
            self.error = "PulseAudio is not installed"
            log.error(self.error)
            return
        conf = self.dir / "wa2sip.pa"
        conf.write_text(PULSE_CONFIG.format(socket=self.socket))
        self.socket.unlink(missing_ok=True)
        p = Supervised("pulseaudio", [
            pulse, "--daemonize=no", "--exit-idle-time=-1", "--use-pid-file=no", "--system=false",
            "--disallow-module-loading=no", "-n", "-F", str(conf), "--log-target=stderr",
            "--log-level=notice"], self.env())
        p.start()
        self.procs.append(p)
        for _ in range(100):
            if self.socket.exists() and await self._pactl("info") is not None:
                log.info("PulseAudio is up (%s)", self.server)
                return
            await asyncio.sleep(0.1)
        self.error = "PulseAudio did not start"
        log.error(self.error)

    async def stop(self) -> None:
        for p in reversed(self.procs):
            await p.stop()
        self.procs = []

    async def _pactl(self, *args: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", f"--server={self.server}", *args, env=self.env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 5)
        except (OSError, TimeoutError):
            return None
        if proc.returncode != 0:
            log.debug("pactl %s failed: %s", " ".join(args), err.decode(errors="replace").strip())
            return None
        return out.decode(errors="replace").strip()

    @staticmethod
    def device_names(account_id: str) -> dict[str, str]:
        base = f"wa_{account_id}"
        return {"speaker": f"{base}_spk", "mic_sink": f"{base}_mic", "mic": f"{base}_in"}

    async def ensure_devices(self, account_id: str) -> dict[str, str]:
        """Create the account's virtual speaker and microphone (idempotent)."""
        names = self.device_names(account_id)
        async with self._lock:
            sinks = await self._pactl("list", "short", "sinks") or ""
            sources = await self._pactl("list", "short", "sources") or ""
            mods: list[int] = self._modules.setdefault(account_id, [])
            if names["speaker"] not in sinks:
                idx = await self._pactl("load-module", "module-null-sink", f"sink_name={names['speaker']}",
                                        "rate=48000", "channels=1",
                                        f"sink_properties=device.description={names['speaker']}")
                if idx and idx.isdigit():
                    mods.append(int(idx))
            if names["mic_sink"] not in sinks:
                idx = await self._pactl("load-module", "module-null-sink", f"sink_name={names['mic_sink']}",
                                        "rate=48000", "channels=1",
                                        f"sink_properties=device.description={names['mic_sink']}")
                if idx and idx.isdigit():
                    mods.append(int(idx))
            if f"\t{names['mic']}\t" not in f"\t{sources}\t".replace("\n", "\t"):
                idx = await self._pactl("load-module", "module-remap-source", f"master={names['mic_sink']}.monitor",
                                        f"source_name={names['mic']}", "channels=1",
                                        f"source_properties=device.description={names['mic']}")
                if idx and idx.isdigit():
                    mods.append(int(idx))
        return names

    async def remove_devices(self, account_id: str) -> None:
        async with self._lock:
            for idx in reversed(self._modules.pop(account_id, [])):
                await self._pactl("unload-module", str(idx))

    def info(self) -> dict:
        return {"error": self.error, "pulse": self.server,
                "processes": {p.name: {"running": p.running, "restarts": p.restarts} for p in self.procs}}
