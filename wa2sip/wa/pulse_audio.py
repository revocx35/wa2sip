"""Call audio through PulseAudio: parec records WhatsApp, pacat feeds its microphone.

Both run as G.711 (alaw/ulaw) at 8 kHz, so no audio is converted in Python: PulseAudio
resamples to and from Chromium's 48 kHz. 20 ms frames (160 bytes) come out of parec.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .base import AudioPipe

log = logging.getLogger("wa2sip.audio")

FORMATS = {"PCMA": "alaw", "PCMU": "ulaw"}
FRAME = 160
MAX_PLAY_BACKLOG = 8000          # bytes queued for pacat (1 s) before we drop instead of buffering


class PulsePipe(AudioPipe):
    def __init__(self, server: str, env: dict, speaker: str, mic_sink: str, codec: str,
                 on_audio: Callable[[bytes], None]):
        self.server = server
        self.env = env
        self.speaker = speaker
        self.mic_sink = mic_sink
        self.codec = codec
        self.on_audio = on_audio
        self.rec: asyncio.subprocess.Process | None = None
        self.play: asyncio.subprocess.Process | None = None
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.dropped = 0
        self.error: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    def _argv(self, mode: str, device: str, latency_ms: int) -> list[str]:
        return ["pacat", f"--{mode}", f"--server={self.server}", f"--device={device}", "--raw",
                f"--format={FORMATS.get(self.codec, 'alaw')}", "--rate=8000", "--channels=1",
                f"--latency-msec={latency_ms}", f"--client-name=wa2sip-{mode}",
                f"--stream-name=wa2sip-{mode}"]

    async def start(self) -> None:
        try:
            self.rec = await asyncio.create_subprocess_exec(
                *self._argv("record", f"{self.speaker}.monitor", 20), "--process-time-msec=10",
                env=self.env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            self.play = await asyncio.create_subprocess_exec(
                *self._argv("playback", self.mic_sink, 60),
                env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            self.error = f"pacat: {e}"
            log.error("call audio could not start: %s", e)
            return
        self._tasks.append(asyncio.create_task(self._read()))
        self._tasks.append(asyncio.create_task(self._watch(self.rec, "record")))
        self._tasks.append(asyncio.create_task(self._watch(self.play, "playback")))

    async def _read(self) -> None:
        assert self.rec and self.rec.stdout
        try:
            while True:
                data = await self.rec.stdout.readexactly(FRAME)
                self.rx_bytes += len(data)
                try:
                    self.on_audio(data)
                except Exception:
                    log.exception("audio handler failed")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _watch(self, proc: asyncio.subprocess.Process, what: str) -> None:
        err = b""
        if proc.stderr:
            err = await proc.stderr.read()
        rc = await proc.wait()
        if not self._closed:
            self.error = f"{what} stopped (code {rc}): {err.decode(errors='replace').strip()[:200]}"
            log.warning("call audio %s", self.error)

    def write(self, payload: bytes) -> None:
        p = self.play
        if self._closed or not p or not p.stdin or p.returncode is not None:
            return
        transport = p.stdin.transport
        if transport.get_write_buffer_size() > MAX_PLAY_BACKLOG:
            self.dropped += len(payload)
            return
        try:
            p.stdin.write(payload)
            self.tx_bytes += len(payload)
        except (ConnectionError, RuntimeError):
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for proc in (self.rec, self.play):
            if proc and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        for t in self._tasks:
            t.cancel()
        for proc in (self.rec, self.play):
            if proc:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 2)

    def info(self) -> dict:
        return {"codec": self.codec, "rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes,
                "dropped": self.dropped, "error": self.error}
