import asyncio

import pytest

from wa2sip.media import g711
from wa2sip.media.rtp import Pacer, RtpPacket, RtpSender
from wa2sip.sip import sdp

OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.5\r\ns=-\r\nc=IN IP4 10.0.0.5\r\nt=0 0\r\n"
    "m=audio 12000 RTP/AVP 0 8 9 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:9 G722/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=sendrecv\r\n"
)


def test_sdp_negotiation_prefers_our_order():
    offer = sdp.Sdp.parse(OFFER)
    neg = sdp.negotiate(offer, ["PCMA", "PCMU"])
    assert neg.codec.name == "PCMA" and neg.remote_pt == 8 and neg.dtmf_pt == 101
    assert offer.audio_address() == ("10.0.0.5", 12000)
    assert sdp.negotiate(offer, ["PCMU"]).codec.name == "PCMU"


def test_sdp_rejects_srtp_and_unknown_codecs():
    assert sdp.negotiate(sdp.Sdp.parse(OFFER.replace("RTP/AVP", "RTP/SAVP")), ["PCMA"]) is None
    only_g722 = OFFER.replace("m=audio 12000 RTP/AVP 0 8 9 101", "m=audio 12000 RTP/AVP 9")
    assert sdp.negotiate(sdp.Sdp.parse(only_g722), ["PCMA", "PCMU"]) is None


def test_sdp_build_parses_back():
    body = sdp.build("192.168.1.2", 16000, [(sdp.PCMA, 8)], 101, 1, 1)
    parsed = sdp.Sdp.parse(body)
    assert parsed.audio_address() == ("192.168.1.2", 16000)
    assert sdp.negotiate_answer(parsed).codec.name == "PCMA"


def test_g711_roundtrip_and_silence():
    for codec in ("PCMA", "PCMU"):
        assert g711.DECODE[codec][g711.SILENCE[codec]] in (0, 8, -8)
        for v in (0, 100, -100, 1000, -5000, 20000, -32000):
            back = g711.DECODE[codec][g711.ENCODE[codec](v)]
            assert abs(back - v) <= max(16, abs(v) * 0.07)
    assert g711.level_dbfs(g711.silence("PCMA"), "PCMA") < -60


def test_g711_translate_and_gain():
    tone_a = g711.tone("PCMA", 1000, 0.1, -20)
    tone_u = g711.convert(tone_a, "PCMA", "PCMU")
    assert abs(g711.level_dbfs(tone_u, "PCMU") - g711.level_dbfs(tone_a, "PCMA")) < 0.5
    louder = g711.convert(tone_a, "PCMA", "PCMA", 6.0)
    assert 5.0 < g711.level_dbfs(louder, "PCMA") - g711.level_dbfs(tone_a, "PCMA") < 7.0
    assert g711.convert(tone_a, "PCMA", "PCMA") is tone_a


def test_rtp_packet_roundtrip():
    s = RtpSender()
    data = s.packet(8, b"\xd5" * 160, 160)
    pkt = RtpPacket.parse(data)
    assert pkt.pt == 8 and pkt.payload == b"\xd5" * 160 and pkt.marker
    pkt2 = RtpPacket.parse(s.packet(8, b"x", 160))
    assert pkt2.seq == (pkt.seq + 1) & 0xFFFF and pkt2.ts == (pkt.ts + 160) & 0xFFFFFFFF
    assert RtpPacket.parse(b"\x00" * 12) is None


def test_pacer_jitter_buffer_logic():
    p = Pacer(lambda f: None, 0xD5)
    assert p.next_frame() == p.silence            # nothing buffered yet
    p.push(b"\x01" * 1024)                        # one bursty camera chunk (128 ms)
    assert p.target == 1024 + 320
    assert p.next_frame() == p.silence            # not primed: below chunk + margin
    p.push(b"\x02" * 1024)
    first = p.next_frame()                         # primes and trims backlog to target
    assert p.primed and len(first) == 160 and len(p.buf) == p.target - 160
    while len(p.buf) >= 160:
        assert len(p.next_frame()) == 160
    assert p.next_frame() == p.silence and p.underruns == 1
    assert p.margin == 480                         # adapts after an underrun
    p.push(b"\x03" * 20000)                       # huge burst -> bounded latency
    assert len(p.buf) <= p.target + p.max_extra


@pytest.mark.asyncio
async def test_pacer_clock_emits_20ms_frames():
    frames = []
    p = Pacer(frames.append, 0xD5)
    p.start()
    for _ in range(3):                             # 3 x 20 ms = chunk + margin -> primes
        p.push(b"\x01" * 160)
    await asyncio.sleep(0.25)
    p.stop()
    assert all(len(f) == 160 for f in frames)
    assert 9 <= len(frames) <= 15
    assert frames.count(b"\x01" * 160) == 3


def _dtmf_tone(digit: str, ms: int = 80) -> list[int]:
    import math
    from wa2sip.media.dtmf import HIGH, KEYS, LOW
    row = next(i for i, r in enumerate(KEYS) if digit in r)
    f1, f2 = LOW[row], HIGH[KEYS[row].index(digit)]
    return [int(7000 * (math.sin(2 * math.pi * f1 * i / 8000) + math.sin(2 * math.pi * f2 * i / 8000)))
            for i in range(8 * ms)]


def test_inband_dtmf_detection_through_g711():
    import math
    import random
    from wa2sip.media import g711
    from wa2sip.media.dtmf import DtmfDetector
    through = lambda xs: [g711.DECODE["PCMU"][g711.linear_to_ulaw(s)] for s in xs]  # noqa: E731
    det = DtmfDetector()
    got = []
    for d in "159*0#":
        for chunk in (through(_dtmf_tone(d)), [0] * 800):
            for i in range(0, len(chunk), 160):          # 20 ms frames like RTP
                r = det.feed(chunk[i:i + 160])
                if r:
                    got.append(r)
    assert "".join(got) == "159*0#"
    held = through(_dtmf_tone("7", 400))                  # a held key is reported once
    det2 = DtmfDetector()
    assert [d for d in (det2.feed(held[i:i + 160]) for i in range(0, len(held), 160)) if d] == ["7"]
    tone = [int(9000 * math.sin(2 * math.pi * 1000 * i / 8000)) for i in range(4000)]
    noise = [random.randint(-8000, 8000) for _ in range(4000)]
    det3 = DtmfDetector()                                 # single tones and noise are not keys
    assert not any(det3.feed(x[i:i + 160]) for x in (tone, noise) for i in range(0, 4000, 160))
