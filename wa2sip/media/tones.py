"""Call progress tones (ringback) as G.711 at 8 kHz."""

from __future__ import annotations

import math
from functools import lru_cache

from . import g711

RATE = 8000

# (frequencies, [(on seconds, off seconds), ...]) per country style
RINGBACK = {
    "eu": ((425.0,), [(1.0, 4.0)]),
    "us": ((440.0, 480.0), [(2.0, 4.0)]),
    "uk": ((400.0, 450.0), [(0.4, 0.2), (0.4, 2.0)]),
}


def _tone(freqs: tuple[float, ...], seconds: float, level_db: float = -16.0) -> list[int]:
    amp = 32767 * 10 ** (level_db / 20) / len(freqs)
    n = int(seconds * RATE)
    fade = int(0.01 * RATE)
    out = []
    for i in range(n):
        v = sum(math.sin(2 * math.pi * f * i / RATE) for f in freqs) * amp
        if i < fade:
            v *= i / fade
        elif i > n - fade:
            v *= (n - i) / fade
        out.append(int(v))
    return out


@lru_cache(maxsize=16)
def ringback(codec: str, style: str = "eu") -> bytes:
    """One full ringback cadence (loop it)."""
    freqs, cadence = RINGBACK.get(style, RINGBACK["eu"])
    samples: list[int] = []
    for on, off in cadence:
        samples += _tone(freqs, on)
        samples += [0] * int(off * RATE)
    return g711.encode_pcm(samples, codec)
