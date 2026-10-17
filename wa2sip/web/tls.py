"""Self-signed certificate for the built-in HTTPS listener (web UI and the integration API).

Integrations (telerouter) pin this certificate's SHA-256, which Settings -> Integrations shows.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

log = logging.getLogger("wa2sip.tls")


def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET: nothing is sent, just picks the LAN interface
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def ensure_certificate(data_dir: str, cert: str = "", key: str = "") -> tuple[str, str] | None:
    """Return (cert, key) paths, generating a self-signed pair in <data>/tls if needed."""
    if cert and key:
        return cert, key
    tls_dir = Path(data_dir) / "tls"
    cert_path, key_path = tls_dir / "cert.pem", tls_dir / "key.pem"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    if not shutil.which("openssl"):
        log.warning("openssl not found - HTTPS disabled (set WA2SIP_TLS_CERT/KEY to enable)")
        return None
    tls_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tls_dir, 0o700)
    sans = ["DNS:localhost", f"DNS:{socket.gethostname()}"] + [f"IP:{ip}" for ip in _local_ips()]
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
             "-keyout", str(key_path), "-out", str(cert_path), "-subj", "/CN=wa2sip",
             "-addext", "subjectAltName=" + ",".join(sans)],
            check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("could not create a self-signed certificate: %s - HTTPS disabled", e)
        return None
    os.chmod(key_path, 0o600)
    log.info("created self-signed HTTPS certificate for %s", ", ".join(sans))
    return str(cert_path), str(key_path)


def fingerprint(cert_path: str) -> str:
    """SHA-256 of the certificate (DER), as hex - what integrations pin."""
    import hashlib
    import ssl
    try:
        pem = Path(cert_path).read_text()
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except (OSError, ValueError):
        return ""
