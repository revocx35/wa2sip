"""Text-to-speech for IVR prompts (espeak-ng, offline) -> 8 kHz G.711.

espeak-ng writes 22.05 kHz 16-bit WAV to stdout; it's low-passed, resampled to
8 kHz, peak-normalised, encoded and cached. Without espeak-ng, prompts fall back
to short beeps so an IVR still works (digits can still be pressed).
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import shutil
import wave
from collections import OrderedDict

from . import g711
from .piper import PiperEngine

log = logging.getLogger("wa2sip.tts")

RATE = 8000
CACHE_SIZE = 128


def _resample_to_8k(samples: array.array, rate: int) -> array.array:
    """Biquad low-pass at 3.4 kHz, then linear-interpolation resampling."""
    if rate == RATE:
        return samples
    w0 = 2 * math.pi * 3400 / rate
    alpha = math.sin(w0) / (2 * math.sqrt(0.5))
    cos = math.cos(w0)
    a0 = 1 + alpha
    b0, b1, b2 = (1 - cos) / 2 / a0, (1 - cos) / a0, (1 - cos) / 2 / a0
    a1, a2 = -2 * cos / a0, (1 - alpha) / a0
    x1 = x2 = y1 = y2 = 0.0
    ratio = rate / RATE
    nxt, prev = 0.0, 0.0
    out = array.array("h")
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        while nxt <= 1.0:
            v = prev + (y - prev) * nxt
            out.append(max(-32768, min(32767, int(v))))
            nxt += ratio
        nxt -= 1.0
        prev = y
    return out


def _normalize(samples: array.array, peak: int = 16000) -> array.array:
    top = max((abs(s) for s in samples), default=0)
    if top == 0:
        return samples
    g = peak / top
    return array.array("h", (int(s * g) for s in samples))


def _parse_wav(data: bytes) -> tuple[array.array, int]:
    # espeak-ng --stdout streams a WAV whose size fields are bogus; read the data
    # chunk until EOF instead of trusting them.
    rate = 22050
    try:
        with wave.open(io.BytesIO(data)) as w:
            rate = w.getframerate()
    except (wave.Error, EOFError):
        pass
    idx = data.find(b"data")
    pcm = data[idx + 8:] if idx >= 0 else b""
    pcm = pcm[: len(pcm) - (len(pcm) % 2)]
    samples = array.array("h")
    samples.frombytes(pcm)
    return samples, rate


PIPER_PREFIX = "piper:"


def piper_key(voice: str) -> str | None:
    return voice[len(PIPER_PREFIX):] if voice.startswith(PIPER_PREFIX) else None


class Tts:
    """Prompt synthesis: Piper voices ("piper:<key>") or espeak-ng voices ("en-us")."""

    def __init__(self, binary: str | None = None, data_dir: str | None = None, piper_port: int = 18556):
        self.binary = binary or shutil.which("espeak-ng") or shutil.which("espeak")
        self._cache: OrderedDict[tuple, array.array] = OrderedDict()
        self._encoded: OrderedDict[tuple, bytes] = OrderedDict()
        self.piper = PiperEngine(data_dir, piper_port) if data_dir else None
        self._espeak_ids: set[str] = set()
        if not self.binary:
            log.warning("espeak-ng not found - IVR prompts will be beeps")

    @property
    def available(self) -> bool:
        return bool(self.binary)

    async def pcm(self, text: str, voice: str = "en-us", speed: int = 150) -> array.array:
        """8 kHz 16-bit samples for `text` (cached)."""
        text = " ".join(text.split())
        key = (text, voice, int(speed))
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        if not text:
            return array.array("h")
        pkey = piper_key(voice)
        if pkey:
            samples = await self._piper(text, pkey, int(speed))
            if samples is None:            # fallback: don't cache, so the natural voice wins later
                return await self._synth(text, await self._espeak_for(pkey), int(speed))
        else:
            samples = await self._synth(text, voice, int(speed))
        self._cache[key] = samples
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return samples

    async def _piper(self, text: str, key: str, speed: int) -> array.array | None:
        if not self.piper or not self.piper.available:
            return None
        try:
            # our "words per minute" scale -> Piper phoneme length (1.0 is its natural pace)
            samples, rate = await self.piper.synthesize(text, key, max(0.5, min(2.0, 165 / max(speed, 1))))
        except Exception as e:
            log.warning("Piper voice %s failed (%s) - using espeak-ng for now", key, e)
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _normalize(_resample_to_8k(samples, rate)))

    async def _espeak_for(self, key: str) -> str:
        """Closest espeak-ng voice for a Piper voice key like 'tr_TR-dfki-medium'."""
        if not self._espeak_ids:
            self._espeak_ids = {v["id"] for v in await self.voices()}
        code = key.split("-")[0]                     # e.g. en_US
        family, _, region = code.partition("_")
        for candidate in (f"{family}-{region.lower()}", family):
            if candidate in self._espeak_ids:
                return candidate
        return "en-us"

    async def close(self) -> None:
        if self.piper:
            await self.piper.close()

    async def _synth(self, text: str, voice: str, speed: int) -> array.array:
        if not self.binary:
            return _beep()
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "-v", voice, "-s", str(speed), "--stdin", "--stdout",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(text.encode()), 20)
        except (OSError, TimeoutError) as e:
            log.warning("TTS failed: %s", e)
            return _beep()
        if proc.returncode != 0 or len(out) < 100:
            log.warning("TTS failed for voice %r: %s", voice, err.decode(errors="replace").strip()[:200])
            return _beep()
        samples, rate = _parse_wav(out)
        loop = asyncio.get_running_loop()
        # CPU-bound resampling of a few seconds of audio: keep it off the event loop
        return await loop.run_in_executor(None, lambda: _normalize(_resample_to_8k(samples, rate)))

    async def encoded(self, text: str, voice: str, speed: int, codec: str) -> bytes:
        key = (" ".join(text.split()), voice, int(speed), codec)
        hit = self._encoded.get(key)
        if hit is not None:
            return hit
        samples = await self.pcm(text, voice, speed)
        enc = g711.ENCODE[codec]
        data = bytes(enc(s) for s in samples)
        self._encoded[key] = data
        if len(self._encoded) > CACHE_SIZE:
            self._encoded.popitem(last=False)
        return data

    async def wav(self, text: str, voice: str, speed: int) -> bytes:
        samples = await self.pcm(text, voice, speed)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.tobytes())
        return buf.getvalue()

    async def voices(self) -> list[dict]:
        if not self.binary:
            return []
        proc = await asyncio.create_subprocess_exec(self.binary, "--voices", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        voices = []
        for line in out.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                voices.append({"id": parts[1], "name": parts[3].replace("_", " ")})
        return voices


def _beep() -> array.array:
    n = int(0.25 * RATE)
    return array.array("h", (int(9000 * math.sin(2 * math.pi * 660 * i / RATE)) for i in range(n)))


def menu_text(greeting: str, option_text: str, options: list[tuple[str, str]]) -> str:
    """Compose the spoken menu, e.g. 'Hello. Press 1 for Front door. Press 2 for Garage.'"""
    parts = [greeting.strip()] if greeting.strip() else []
    for digit, name in options:
        parts.append(fill(option_text, digit=digit, name=name))
    return " ".join(parts)


def fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out
