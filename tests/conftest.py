import socket

import pytest


def free_port(kind=socket.SOCK_DGRAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def no_voice_downloads(monkeypatch):
    """Tests never reach Hugging Face: the Piper catalog is unavailable unless a test fakes it."""
    from wa2sip.media import piper

    async def offline(self):
        raise RuntimeError("voice catalog unavailable in tests")
    monkeypatch.setattr(piper.PiperEngine, "catalog", offline)
