"""Admin password hashing and signed session cookies (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE = "wa2sip_session"
SESSION_TTL = 7 * 24 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, pw_hash: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    # binding the password hash means a password change logs out old sessions
    return f"{exp}.{_sign(secret, f'{exp}:{pw_hash}')}"


def check_session(token: str | None, secret: str, pw_hash: str) -> bool:
    if not token or "." not in token or not pw_hash:
        return False
    exp_s, sig = token.split(".", 1)
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(secret, f"{exp_s}:{pw_hash}"))


MIN_PASSWORD_LENGTH = 10


class Locked(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"locked for {retry_after:.0f} s")
        self.retry_after = retry_after


class LoginThrottle:
    """Brute-force protection for admin password checks (in memory; a restart clears it).

    - Per client IP: after IP_THRESHOLD failures, locks that grow exponentially (30 s ... 1 h).
    - Global: after GLOBAL_THRESHOLD consecutive failures from all IPs together, one attempt per
      lock period (30 s ... 15 min). This bounds distributed guessing to about a hundred tries a day.

    `begin()` charges the attempt *before* the password is checked and `success()` refunds it, so
    parallel requests can't race past a lock.
    """

    IP_THRESHOLD = 5
    IP_BASE, IP_MAX = 30.0, 3600.0
    GLOBAL_THRESHOLD = 50
    GLOBAL_BASE, GLOBAL_MAX = 30.0, 900.0
    IDLE_RESET = 24 * 3600.0     # failures from a quiet IP are forgotten after a day

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._ips: dict[str, list[float]] = {}   # ip -> [failures, locked_until, last_attempt]
        self._global_fails = 0
        self._global_locked_until = 0.0

    @staticmethod
    def _backoff(n: int, threshold: int, base: float, cap: float) -> float:
        return min(base * 2 ** max(0, n - threshold - 1), cap)

    def begin(self, ip: str) -> None:
        """Charge one attempt. Raises Locked when the IP or the account is locked."""
        now = self._clock()
        if len(self._ips) > 10_000:
            self._ips = {k: v for k, v in self._ips.items() if v[1] > now or now - v[2] < self.IDLE_RESET}
        st = self._ips.get(ip)
        if st and st[1] <= now and now - st[2] > self.IDLE_RESET:
            st = None
        if st and st[1] > now:
            raise Locked(st[1] - now)
        if self._global_locked_until > now:
            raise Locked(self._global_locked_until - now)

        self._global_fails += 1
        if self._global_fails > self.GLOBAL_THRESHOLD:
            self._global_locked_until = now + self._backoff(
                self._global_fails, self.GLOBAL_THRESHOLD, self.GLOBAL_BASE, self.GLOBAL_MAX)
        st = st or [0, 0.0, now]
        st[0] += 1
        st[2] = now
        if st[0] > self.IP_THRESHOLD:
            st[1] = now + self._backoff(int(st[0]), self.IP_THRESHOLD, self.IP_BASE, self.IP_MAX)
        self._ips[ip] = st

    def success(self, ip: str) -> None:
        """The password was right: clear the counters charged by begin()."""
        self._ips.pop(ip, None)
        self._global_fails = 0
        self._global_locked_until = 0.0
