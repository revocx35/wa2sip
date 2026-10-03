#!/usr/bin/env python3
"""Check the audio path Chromium <-> PulseAudio <-> wa2sip without WhatsApp.

Starts Xvfb + PulseAudio (like the app), creates one account's virtual devices, opens a
Chromium page that plays its microphone back to its speaker (agent/loopback.js), sends a
1 kHz tone in through pacat and measures what comes back out of parec: level, frequency,
latency. Run it inside the image:

    docker run --rm wa2sip:dev python tools/audio_loopback.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wa2sip.media import g711  # noqa: E402
from wa2sip.wa.pulse_audio import PulsePipe  # noqa: E402
from wa2sip.wa.manager import sandbox_supported  # noqa: E402
from wa2sip.wa.system import System  # noqa: E402


def goertzel(samples: list[int], freq: float, rate: int = 8000) -> float:
    k = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - k * s1 * s2


async def main() -> int:
    agent_dir = os.environ.get("WA2SIP_AGENT_DIR", "/app/agent")
    system = System("/tmp/wa2sip-loop", display=":98", use_xvfb=os.environ.get("WA2SIP_XVFB", "1") == "1")
    await system.start()
    if system.error:
        print("FAIL:", system.error)
        return 1
    dev = await system.ensure_devices("loop")
    env = system.env()
    sandbox = os.environ.get("WA2SIP_SANDBOX") or ("1" if await sandbox_supported() else "0")
    print("Chromium sandbox:", "on" if sandbox == "1" else "off (this container can't create user namespaces)")
    env.update({"PULSE_SINK": dev["speaker"], "PULSE_SOURCE": dev["mic"], "WA2SIP_SANDBOX": sandbox,
                "WA2SIP_HEADLESS": "0" if system.use_xvfb else "1"})
    node = await asyncio.create_subprocess_exec(
        "node", str(Path(agent_dir) / "loopback.js"), cwd=agent_dir, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    line = await asyncio.wait_for(node.stdout.readline(), 60)
    msg = json.loads(line)
    if "error" in msg:
        print("FAIL: browser:", msg["error"])
        return 1
    print("browser microphone:", msg["info"]["label"], "| settings:", json.dumps(msg["info"]["settings"]))
    received = bytearray()
    pipe = PulsePipe(system.server, system.env(), dev["speaker"], dev["mic_sink"], "PCMA", received.extend)
    await pipe.start()
    await asyncio.sleep(1.0)
    received.clear()
    tone = g711.tone("PCMA", 1000.0, 2.0, -10.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    t = t0
    for i in range(0, len(tone), 160):
        pipe.write(tone[i:i + 160])
        t += 0.02
        await asyncio.sleep(max(0.0, t - loop.time()))
    await asyncio.sleep(1.0)
    await pipe.close()
    node.stdin.close()
    await asyncio.wait_for(node.wait(), 15)
    await system.stop()

    dec = g711.DECODE["PCMA"]
    pcm = [dec[b] for b in received]
    blocks = [pcm[i:i + 400] for i in range(0, len(pcm) - 399, 400)]    # 50 ms
    levels = [round(g711.level_dbfs(bytes(received[i * 400:(i + 1) * 400]), "PCMA"), 1) for i in range(len(blocks))]
    first = next((i for i, lv in enumerate(levels) if lv > -40), None)
    print("received", len(received), "bytes; level per 50 ms:", levels)
    if first is None:
        print("FAIL: the tone did not come back")
        return 1
    loud = [b for b, lv in zip(blocks, levels) if lv > -40]
    sample = [x for b in loud[2:-2] or loud for x in b]
    p1k = goertzel(sample, 1000)
    other = max(goertzel(sample, f) for f in (500, 700, 1300, 1600, 2000))
    print(f"tone came back after ~{first * 50} ms (50 ms resolution), level {max(levels):.1f} dBFS "
          f"(sent -10.0), 1 kHz dominance {10 * math.log10(p1k / max(other, 1e-9)):.0f} dB")
    if p1k < other * 10:
        print("FAIL: received audio is not the 1 kHz tone")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
