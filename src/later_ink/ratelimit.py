"""Per-IP rate limiters.

Two kinds, because the three things being limited have genuinely different
requirements:

`DurableLimiter` keeps its counters in SQLite, so they survive a machine
stop/start and are shared across instances. That matters for the two limits
that are about abuse: guessing catalog secrets, and creating users. An
in-process counter would reset on every cold start, which on a
scale-to-zero host is an attacker-controlled event.

`MemoryLimiter` keeps its counters in this process. That's the right trade for
feed traffic, which is high-volume and where the goal is keeping one client
from hammering the upstream API — not enforcing a security boundary. Putting
that on SQLite would mean a write on every catalog request, and store.py is
explicit that the read paths must not queue behind the writer (see the
comment on record_hit). A limit that resets on restart is fine here; a
catalog feed that serializes behind a write lock is not.
"""

import time
from collections import OrderedDict, deque

from .store import Store


class DurableLimiter:
    """Per-IP limiter backed by the SQLite store.

    After `limit` recorded events in `window` seconds, an IP is blocked until
    its traffic ages out. `bucket` namespaces the counters so unrelated limits
    don't share a budget.
    """

    def __init__(self, store: Store, bucket: str, limit: int, window: float):
        self.store = store
        self.bucket = bucket
        self.limit = limit
        self.window = window

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def try_record(self, ip: str) -> bool:
        """Consume one unit of this IP's budget. False if it had none left.

        The admission decision — this method — is a single transaction. Use it
        rather than blocked()+record(), which lets concurrent instances all
        read the same under-limit count before any of them writes.
        """
        if not self.enabled:
            return True
        return self.store.try_record_event(self.bucket, ip, self.limit, self.window)

    def blocked(self, ip: str) -> bool:
        """Best-effort read-only check, for skipping work before doing it.

        Deliberately not the admission decision: it's a plain read, so two
        callers can both see "under limit" at once. Anything that needs to be
        counted must go through try_record.
        """
        if not self.enabled:
            return False
        return self.store.event_count(self.bucket, ip, self.window) >= self.limit


class MemoryLimiter:
    """Per-IP sliding window held in this process.

    `max_ips` bounds memory: the IP is either the socket peer or a header from
    a proxy we've been told to trust, but neither is a reason to let the table
    grow without limit. Least-recently-seen entries are evicted first, which
    means a flood of one-off addresses can push out an established offender's
    counter — acceptable, because those same addresses are each getting their
    own limit anyway.
    """

    def __init__(self, limit: int, window: float, max_ips: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_ips = max_ips
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def allow(self, ip: str) -> bool:
        """Record a hit and report whether it was within the limit."""
        if not self.enabled:
            return True
        now = time.monotonic()
        cutoff = now - self.window
        hits = self._hits.get(ip)
        if hits is None:
            hits = deque()
            self._hits[ip] = hits
        while hits and hits[0] < cutoff:
            hits.popleft()
        self._hits.move_to_end(ip)
        while len(self._hits) > self.max_ips:
            self._hits.popitem(last=False)
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
