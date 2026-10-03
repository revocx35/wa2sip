"""Process settings from environment variables (all prefixed WA2SIP_)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

from . import __version__


def _env(name: str, default: str) -> str:
    return os.environ.get(f"WA2SIP_{name}", default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


# Same set as Caddy's `private_ranges`: loopback and private networks.
PRIVATE_RANGES = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"]


def parse_trusted_proxies(value: str) -> list[str]:
    """WA2SIP_TRUSTED_PROXIES: comma-separated IPs/networks, `private` or `none`.

    Only these peers may set X-Forwarded-For/-Proto. The client address is the right-most
    X-Forwarded-For entry that is not a trusted proxy, so a client can't spoof it by sending
    its own header. `*` is refused: it makes the left-most, client-controlled entry count.
    """
    out: list[str] = []
    for item in (x.strip() for x in value.split(",")):
        if not item or item.lower() == "none":
            continue
        if item.lower() == "private":
            out += PRIVATE_RANGES
            continue
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ValueError(f"WA2SIP_TRUSTED_PROXIES: {item!r} is not an IP address or network "
                             "(use addresses/CIDRs, 'private' or 'none')") from None
    return out


def _cookie_mode(value: str) -> str:
    v = value.lower()
    if v in ("1", "true", "yes"):
        return "true"
    if v in ("0", "false", "no"):
        return "false"
    return "auto"


@dataclass
class Settings:
    version: str = __version__
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data"))
    web_host: str = field(default_factory=lambda: _env("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: _int("WEB_PORT", 8092))
    sip_bind: str = field(default_factory=lambda: _env("SIP_BIND", "0.0.0.0"))
    sip_port: int = field(default_factory=lambda: _int("SIP_PORT", 5064))
    advertise_ip: str = field(default_factory=lambda: _env("ADVERTISE_IP", ""))
    rtp_port_min: int = field(default_factory=lambda: _int("RTP_PORT_MIN", 17000))
    rtp_port_max: int = field(default_factory=lambda: _int("RTP_PORT_MAX", 17199))
    piper_port: int = field(default_factory=lambda: _int("PIPER_PORT", 18566))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    sip_trace: bool = field(default_factory=lambda: _bool("SIP_TRACE", False))
    # auto: Secure when a trusted reverse proxy reports HTTPS (X-Forwarded-Proto); true / false: always / never
    secure_cookies: str = field(default_factory=lambda: _cookie_mode(_env("SECURE_COOKIES", "auto")))
    trusted_proxies: list[str] = field(default_factory=lambda: parse_trusted_proxies(_env("TRUSTED_PROXIES", "private")))

    # -- WhatsApp side -------------------------------------------------------------------
    # chromium: real WhatsApp Web in Chromium (production). fake: simulated accounts (tests, demos).
    wa_driver: str = field(default_factory=lambda: _env("WA_DRIVER", "chromium").lower())
    node_bin: str = field(default_factory=lambda: _env("NODE_BIN", "node"))
    agent_dir: str = field(default_factory=lambda: _env("AGENT_DIR", "/app/agent"))
    chromium_path: str = field(default_factory=lambda: _env("CHROMIUM_PATH", "/usr/bin/chromium"))
    # auto: use Chromium's sandbox when the container allows user namespaces, else --no-sandbox
    chromium_sandbox: str = field(default_factory=lambda: _env("CHROMIUM_SANDBOX", "auto").lower())
    # true: headful Chromium on a private Xvfb display (closest to a real desktop browser)
    use_xvfb: bool = field(default_factory=lambda: _bool("XVFB", True))
    display: str = field(default_factory=lambda: _env("DISPLAY", ":99"))
    runtime_dir: str = field(default_factory=lambda: _env("RUNTIME_DIR", "/tmp/wa2sip"))
    # turn off Chromium's echo cancellation / noise suppression / AGC on the virtual microphone:
    # the "microphone" is a clean digital feed from the PBX, so there is no echo to cancel
    raw_mic: bool = field(default_factory=lambda: _bool("RAW_MIC", True))
    wa_trace: bool = field(default_factory=lambda: _bool("WA_TRACE", False))
