import time
from collections import deque


class MissLimiter:
    """Per-IP limiter for unknown-secret lookups.

    Secrets are short enough to be guessable in principle, so failed lookups
    are the thing to throttle: after `limit` misses in `window` seconds, an IP
    only sees 429s until traffic ages out.
    """

    def __init__(self, limit: int = 20, window: float = 3600.0, max_ips: int = 10000):
        self.limit = limit
        self.window = window
        self.max_ips = max_ips
        self._misses: dict[str, deque[float]] = {}

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._misses.setdefault(ip, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._prune(ip, time.monotonic())) >= self.limit

    def record_miss(self, ip: str) -> None:
        now = time.monotonic()
        if len(self._misses) > self.max_ips:
            # Drop stale entries wholesale rather than grow unbounded
            self._misses = {
                k: q for k, q in self._misses.items()
                if q and now - q[-1] <= self.window
            }
        self._prune(ip, now).append(now)
