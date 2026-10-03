import array
import asyncio
import io
import json
import math
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from wa2sip.media import piper as piper_mod
from wa2sip.media.piper import PiperEngine, simplify
from wa2sip.media.tts import Tts
from wa2sip.settings import Settings
from wa2sip.web.app import create_app

from .conftest import free_port

CATALOG = {
    "tr_TR-dfki-medium": {
        "key": "tr_TR-dfki-medium", "name": "dfki", "quality": "medium", "num_speakers": 1,
        "language": {"code": "tr_TR", "family": "tr", "region": "TR", "name_native": "Türkçe",
                     "name_english": "Turkish", "country_english": "Turkey"},
        "files": {"tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx": {"size_bytes": 4000},
                  "tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json": {"size_bytes": 100},
                  "tr/tr_TR/dfki/medium/MODEL_CARD": {"size_bytes": 10}},
    },
}
VOICE_JSON = {"audio": {"sample_rate": 22050, "quality": "medium"}, "num_speakers": 1,
              "language": CATALOG["tr_TR-dfki-medium"]["language"]}


def fake_hf(requests: list):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=b"\0" * 4000)
        if request.url.path.endswith(".onnx.json"):
            return httpx.Response(200, json=VOICE_JSON)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_simplify():
    s = simplify("tr_TR-dfki-medium", CATALOG["tr_TR-dfki-medium"])
    assert s["family"] == "tr" and s["language"] == "Turkish" and s["native"] == "Türkçe"
    assert s["size_mb"] == 0.0 and s["quality"] == "medium"


@pytest.mark.asyncio
async def test_download_install_delete(tmp_path, monkeypatch):
    eng = PiperEngine(tmp_path)
    requests = []
    eng.http = httpx.AsyncClient(transport=fake_hf(requests))

    async def catalog(self):
        return CATALOG
    monkeypatch.setattr(PiperEngine, "catalog", catalog)
    assert not eng.is_installed("tr_TR-dfki-medium")
    # two concurrent callers share one download
    await asyncio.gather(eng.download("tr_TR-dfki-medium"), eng.download("tr_TR-dfki-medium"))
    assert eng.is_installed("tr_TR-dfki-medium")
    assert len(requests) == 2 and not any("MODEL_CARD" in r for r in requests)
    assert eng.downloads == {}
    inst = eng.installed()
    assert inst[0]["key"] == "tr_TR-dfki-medium" and inst[0]["family"] == "tr"
    with pytest.raises(ValueError):
        await eng.download("xx_XX-nope-low")
    eng.delete("tr_TR-dfki-medium")
    assert not eng.is_installed("tr_TR-dfki-medium") and eng.installed() == []
    await eng.close()


@pytest.mark.asyncio
async def test_tts_uses_piper_and_falls_back(tmp_path):
    tts = Tts(data_dir=str(tmp_path))
    # no catalog (offline) -> the natural voice can't be downloaded -> espeak fallback, not cached
    pcm = await tts.pcm("Merhaba", "piper:tr_TR-dfki-medium", 150)
    assert len(pcm) > 0
    assert not any(k[1].startswith("piper:") for k in tts._cache)
    assert await tts._espeak_for("tr_TR-dfki-medium") == "tr"
    assert await tts._espeak_for("en_GB-alba-medium") == "en-gb"

    class FakePiper:
        available = True
        calls = []

        async def synthesize(self, text, key, length_scale):
            self.calls.append((text, key, length_scale))
            n = 22050
            return array.array("h", (int(8000 * math.sin(2 * math.pi * 300 * i / 22050)) for i in range(n))), 22050

        async def close(self):
            pass
    tts.piper = FakePiper()
    pcm = await tts.pcm("Merhaba", "piper:tr_TR-dfki-medium", 150)
    assert abs(len(pcm) - 8000) <= 2 and max(abs(x) for x in pcm) == 16000   # 1 s at 8 kHz, normalised
    assert tts.piper.calls[0][1] == "tr_TR-dfki-medium" and abs(tts.piper.calls[0][2] - 1.1) < 0.01
    assert ("Merhaba", "piper:tr_TR-dfki-medium", 150) in tts._cache
    await tts.pcm("Merhaba", "piper:tr_TR-dfki-medium", 150)
    assert len(tts.piper.calls) == 1                                        # cached


def test_voices_api(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), piper_port=free_port(), wa_driver="fake",
                 rtp_port_min=31400, rtp_port_max=31419)
    with TestClient(create_app(s)) as c:
        c.post("/api/setup", json={"password": "secret1234"})
        engine = c.app.state.engine
        piper = engine.tts.piper

        async def catalog(self):
            return CATALOG
        monkeypatch.setattr(PiperEngine, "catalog", catalog)
        started = []
        monkeypatch.setattr(piper, "start_download", lambda key: started.append(key))
        v = c.get("/api/tts/voices").json()
        assert v["piper"]["available"] is piper_mod.available()
        if not v["piper"]["available"]:
            pytest.skip("piper-tts not installed")
        assert v["piper"]["catalog"][0]["key"] == "tr_TR-dfki-medium"
        assert any(x["id"] == "tr" for x in v["espeak"]["voices"]) or not v["espeak"]["available"]
        assert c.post("/api/tts/voices/tr_TR-dfki-medium").status_code == 200 and started == ["tr_TR-dfki-medium"]
        assert c.post("/api/tts/voices/nope").status_code == 404
        # install by hand, use it as the default voice, then deletion is blocked
        (piper.dir / "tr_TR-dfki-medium.onnx").write_bytes(b"\0")
        (piper.dir / "tr_TR-dfki-medium.onnx.json").write_text(json.dumps(VOICE_JSON))
        assert c.put("/api/settings", json={"default_voice": "piper:tr_TR-dfki-medium"}).status_code == 200
        inst = c.get("/api/tts/voices").json()["piper"]["installed"]
        assert inst[0]["used_by"] == ["default voice"]
        assert c.delete("/api/tts/voices/tr_TR-dfki-medium").status_code == 409
        c.put("/api/settings", json={"default_voice": "tr"})
        assert c.delete("/api/tts/voices/tr_TR-dfki-medium").status_code == 200
