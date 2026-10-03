"""PIN checks for bridges, with brute-force protection.

A bridge PIN is short, and anyone who can call the extension can guess, so wrong PINs are
counted per bridge across calls (in memory; a restart clears it). After FREE_FAILURES wrong
PINs in a row the bridge's PIN is locked: 60 s, doubling with every further wrong PIN up to an
hour. While locked every PIN is refused, the right one too, and callers just hear "wrong PIN".
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable

log = logging.getLogger("wa2sip.pin")

FREE_FAILURES = 10
FIRST_LOCK = 60.0
MAX_LOCK = 3600.0


class PinGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, key: str) -> float:
        return max(0.0, self._locked_until.get(key, 0.0) - self.clock())

    def check(self, key: str, entered: str, pin: str, who: str = "") -> bool:
        """True when `entered` is the PIN and the bridge isn't locked."""
        if self.locked_for(key) > 0:
            log.warning("PIN for bridge %s is locked (%.0f s left) - refused a PIN from %s",
                        key, self.locked_for(key), who or "?")
            return False
        if pin and hmac.compare_digest(entered.encode(), pin.encode()):
            self._failures.pop(key, None)
            return True
        n = self._failures.get(key, 0) + 1
        self._failures[key] = n
        if n >= FREE_FAILURES:
            lock = min(FIRST_LOCK * 2 ** (n - FREE_FAILURES), MAX_LOCK)
            self._locked_until[key] = self.clock() + lock
            log.warning("%d wrong PINs in a row on bridge %s (last from %s) - PIN locked for %.0f s",
                        n, key, who or "?", lock)
        return False
