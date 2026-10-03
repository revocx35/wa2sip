"""Natural-sounding neural text-to-speech with Piper (offline, CPU).

Piper (https://github.com/OHF-Voice/piper1-gpl, GPL-3.0) runs as a separate
process - its own HTTP server bound to 127.0.0.1 - which keeps voices loaded
in memory (~0.2 s per prompt). Voices come from the rhasspy/piper-voices
catalog on Hugging Face and are downloaded on demand into <data>/voices.
"""

from __future__ import annotations

import array
import asyncio
import importlib.util
import io
import json
import logging
import os
import sys
import time
import wave
from pathlib import Path

import httpx

log = logging.getLogger("wa2sip.piper")

HF = "https://huggingface.co/rhasspy/piper-voices"
CATALOG_URL = f"{HF}/raw/main/voices.json"
FILE_URL = f"{HF}/resolve/main/"
CATALOG_TTL = 24 * 3600


def available() -> bool:
    return importlib.util.find_spec("piper") is not None and importlib.util.find_spec("flask") is not None


def simplify(key: str, entry: dict) -> dict:
    lang = entry.get("language", {})
    onnx = next((f for p, f in entry.get("files", {}).items() if p.endswith(".onnx")), {})
    return {
        "key": key,
        "family": lang.get("family", key.split("_")[0]),
        "code": lang.get("code", ""),
        "language": lang.get("name_english", ""),
        "native": lang.get("name_native", ""),
        "country": lang.get("country_english", ""),
        "name": entry.get("name", key),
        "quality": entry.get("quality", ""),
        "speakers": entry.get("num_speakers", 1),
        "size_mb": round(onnx.get("size_bytes", 0) / 1e6, 1),
    }


class PiperEngine:
    def __init__(self, data_dir: str | Path, port: int = 18556):
        self.dir = Path(data_dir) / "voices"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.available = available()
        self.downloads: dict[str, dict] = {}          # key -> {"progress": 0..1, "error": str|None}
        self._download_tasks: dict[str, asyncio.Task] = {}
        self._catalog: dict[str, dict] | None = None
        self._catalog_at = 0.0
        self.catalog_error: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._server_lock = asyncio.Lock()
        self._log_task: asyncio.Task | None = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))

    # -- voices on disk ---------------------------------------------------------------
    def installed(self) -> list[dict]:
        out = []
        for onnx in sorted(self.dir.glob("*.onnx")):
            cfg_path = onnx.with_suffix(".onnx.json")
            if not cfg_path.exists():
                continue
            try:
                cfg = json.loads(cfg_path.read_text())
            except ValueError:
                continue
            entry = simplify(onnx.stem, cfg)
            parts = onnx.stem.split("-")            # <lang>_<REGION>-<name>-<quality>
            entry["name"] = cfg.get("dataset") or (parts[1] if len(parts) >= 3 else onnx.stem)
            entry["size_mb"] = round(onnx.stat().st_size / 1e6, 1)
            entry["quality"] = cfg.get("audio", {}).get("quality") or (parts[-1] if len(parts) >= 3 else "")
            out.append(entry)
        return out

    def is_installed(self, key: str) -> bool:
        return (self.dir / f"{key}.onnx").exists() and (self.dir / f"{key}.onnx.json").exists()

    def delete(self, key: str) -> None:
        for suffix in (".onnx", ".onnx.json"):
            (self.dir / f"{key}{suffix}").unlink(missing_ok=True)

    # -- catalog -------------------------------------------------------------------------
    async def catalog(self) -> dict[str, dict]:
        """The rhasspy/piper-voices catalog (cached for a day, also on disk for offline use)."""
        if self._catalog is not None and time.time() - self._catalog_at < CATALOG_TTL:
            return self._catalog
        cache = self.dir / "voices.json"
        try:
            r = await self.http.get(CATALOG_URL, follow_redirects=True, timeout=20)
            r.raise_for_status()
            data = r.json()
            cache.write_text(json.dumps(data))
            self.catalog_error = None
        except Exception as e:
            self.catalog_error = f"voice catalog unavailable: {e}"
            if self._catalog is not None:
                return self._catalog
            if not cache.exists():
                raise RuntimeError(self.catalog_error) from e
            data = json.loads(cache.read_text())
        self._catalog = data
        self._catalog_at = time.time()
        return data

    # -- downloads -------------------------------------------------------------------------
    def start_download(self, key: str) -> asyncio.Task | None:
        """Start (or join) the background download of a voice."""
        if self.is_installed(key):
            return None
        task = self._download_tasks.get(key)
        if not task or task.done():
            task = self._download_tasks[key] = asyncio.create_task(self._download(key))
            task.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                                   log.warning("voice %s download failed: %s", key, t.exception()))
        return task

    async def download(self, key: str) -> None:
        """Download a voice (.onnx + .onnx.json); concurrent callers share one download."""
        task = self.start_download(key)
        if task:
            await asyncio.shield(task)

    async def _download(self, key: str) -> None:
        catalog = await self.catalog()
        entry = catalog.get(key)
        if not entry:
            raise ValueError(f"unknown voice {key}")
        files = [p for p in entry["files"] if p.endswith((".onnx", ".onnx.json"))]
        total = sum(entry["files"][p].get("size_bytes", 0) for p in files) or 1
        state = self.downloads[key] = {"progress": 0.0, "error": None}
        done = 0
        log.info("downloading voice %s (%.0f MB)", key, total / 1e6)
        try:
            for path in sorted(files, key=lambda p: p.endswith(".onnx.json")):   # model first, config last
                target = self.dir / Path(path).name
                tmp = target.with_name(target.name + ".part")
                async with self.http.stream("GET", FILE_URL + path, follow_redirects=True, timeout=None) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            done += len(chunk)
                            state["progress"] = min(0.99, done / total)
                os.replace(tmp, target)
            state["progress"] = 1.0
            log.info("voice %s installed", key)
        except Exception as e:
            state["error"] = str(e) or e.__class__.__name__
            for path in files:
                (self.dir / (Path(path).name + ".part")).unlink(missing_ok=True)
            raise
        finally:
            if state["error"] is None:
                self.downloads.pop(key, None)

    # -- server ------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _healthy(self) -> bool:
        try:
            r = await self.http.get(f"{self.url}/voices", timeout=2)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def ensure_server(self, voice: str) -> None:
        async with self._server_lock:
            if self._proc and self._proc.returncode is None and await self._healthy():
                return
            if self._proc and self._proc.returncode is None:
                self._proc.kill()
            env = dict(os.environ, OMP_NUM_THREADS=os.environ.get("OMP_NUM_THREADS", "2"))
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "piper.http_server", "-m", voice, "--data-dir", str(self.dir),
                "--host", "127.0.0.1", "--port", str(self.port),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
            self._log_task = asyncio.create_task(self._pump_logs(self._proc))
            for _ in range(120):
                if self._proc.returncode is not None:
                    raise RuntimeError("Piper server exited - see logs")
                if await self._healthy():
                    log.info("Piper TTS server started (voice %s)", voice)
                    return
                await asyncio.sleep(0.25)
            raise RuntimeError("Piper server did not start")

    async def _pump_logs(self, proc) -> None:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and "pthread_setaffinity_np" not in line and "development server" not in line:
                log.debug("piper: %s", line)

    async def synthesize(self, text: str, key: str, length_scale: float = 1.0) -> tuple[array.array, int]:
        """Samples (int16) and sample rate for `text` in voice `key` (downloads it if needed)."""
        if not self.available:
            raise RuntimeError("Piper is not installed")
        if not self.is_installed(key):
            await self.download(key)
        await self.ensure_server(key)
        r = await self.http.post(f"{self.url}/synthesize", timeout=60,
                                 json={"text": text, "voice": key, "length_scale": round(length_scale, 3)})
        r.raise_for_status()
        with wave.open(io.BytesIO(r.content)) as w:
            rate = w.getframerate()
            samples = array.array("h")
            samples.frombytes(w.readframes(w.getnframes()))
        return samples, rate

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
        if self._log_task:
            self._log_task.cancel()
        await self.http.aclose()
