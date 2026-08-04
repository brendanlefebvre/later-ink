from .store import Store


class MissLimiter:
    """Per-IP limiter for unknown-secret lookups, backed by the SQLite store.

    Secrets are short enough to be guessable in principle, so failed lookups
    are the thing to throttle: after `limit` misses in `window` seconds, an IP
    only sees 429s until traffic ages out. State lives in SQLite so it
    survives machine cold starts and is shared across instances.
    """

    def __init__(self, store: Store, limit: int = 20, window: float = 3600.0):
        self.store = store
        self.limit = limit
        self.window = window

    def blocked(self, ip: str) -> bool:
        return self.store.miss_count(ip, self.window) >= self.limit

    def record_miss(self, ip: str) -> None:
        self.store.record_miss(ip, self.window)
