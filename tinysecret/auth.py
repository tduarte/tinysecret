"""Single-password login, in-memory sessions and failed-login rate limiting."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class Session:
    expires: float
    csrf: str


class SessionStore:
    """Server-side sessions. A restart logs everyone out, which is fine for one user."""

    def __init__(self, ttl_seconds: int = 7 * 24 * 3600):
        self.ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        s = Session(expires=time.time() + self.ttl, csrf=secrets.token_urlsafe(24))
        with self._lock:
            self._purge()
            self._sessions[token] = s
        return token, s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if s.expires < time.time():
                del self._sessions[token]
                return None
            s.expires = time.time() + self.ttl  # sliding expiry
            return s

    def delete(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _purge(self) -> None:
        now = time.time()
        for t in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[t]


@dataclass
class _Bucket:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginLimiter:
    """Lock an IP out after `max_failures` failures within `window` seconds.

    A global limit also applies, so rotating source addresses doesn't help much.
    """

    def __init__(self, max_failures: int = 5, window: int = 900, lockout: int = 900,
                 global_max: int = 30):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.global_max = global_max
        self._buckets: dict[str, _Bucket] = {}
        self._global = _Bucket()
        self._lock = threading.Lock()

    def retry_after(self, ip: str) -> int:
        """Seconds until this IP may try again (0 = allowed now)."""
        now = time.time()
        with self._lock:
            b = self._buckets.get(ip)
            wait = max(b.locked_until if b else 0, self._global.locked_until) - now
        return max(0, int(wait + 0.999))

    def failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            for bucket, limit in ((self._buckets.setdefault(ip, _Bucket()), self.max_failures),
                                  (self._global, self.global_max)):
                bucket.failures = [t for t in bucket.failures if t > now - self.window]
                bucket.failures.append(now)
                if len(bucket.failures) >= limit:
                    bucket.locked_until = now + self.lockout
                    bucket.failures.clear()

    def success(self, ip: str) -> None:
        with self._lock:
            self._buckets.pop(ip, None)
