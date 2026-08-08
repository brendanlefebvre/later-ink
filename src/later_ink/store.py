import ipaddress
import logging
import os
import secrets as pysecrets
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from .words import WORDS

logger = logging.getLogger(__name__)


def _clean_referer(referer: str | None) -> str | None:
    """Normalize a stored Referer to scheme://host/path, dropping query and
    fragment. A referer URL can carry query-string PII from the *sending* site;
    the path is kept because it's useful attribution (e.g. which subreddit).
    Capped as a backstop against an oversized header."""
    if not referer:
        return None
    referer = referer.strip()
    if not referer:
        return None
    try:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            # "No IPs" is a product claim; a referer with an IP-literal host
            # would persist an address. Drop it rather than store one.
            try:
                ipaddress.ip_address(parts.hostname or "")
                return None
            except ValueError:
                pass
            referer = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        pass
    return referer[:500]

RESERVED_PATHS = {
    "opds", "start", "health", "healthz", "version", "stats", "static", "assets",
    "docs", "openapi.json", "favicon.ico", "robots.txt",
}

# Four words ≈ 35 bits over the 419-word list — guessable-any-user math stops
# working while the URL stays typeable on an e-ink keyboard.
SECRET_WORD_COUNT = 4


def generate_secret() -> str:
    return "-".join(pysecrets.choice(WORDS) for _ in range(SECRET_WORD_COUNT))


class Store:
    """SQLite-backed user store.

    Readwise tokens are Fernet-encrypted at rest: possession of the database
    file alone must not be sufficient to read them. The key lives in the
    environment (never the image or the volume); losing it means every user
    re-onboards — an accepted trade.
    """

    def __init__(self, path: str, fernet: Fernet):
        self.path = path
        self._fernet = fernet
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    secret TEXT PRIMARY KEY,
                    readwise_token TEXT NOT NULL,
                    stripe_ref TEXT UNIQUE,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_events (
                    bucket TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS rate_events_lookup"
                " ON rate_events (bucket, ip, ts)"
            )
            # Superseded by rate_events, which adds the bucket column. Dropped
            # rather than left behind: the rows were per-IP counters with an
            # hour-long window, so anything in there had already expired, and a
            # dead table invites someone to write to the wrong one.
            conn.execute("DROP TABLE IF EXISTS misses")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hits (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    path TEXT,
                    referer TEXT,
                    user_agent TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS hits_ts ON hits (ts)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # WAL so a writer (rate-limit miss, referrer-log hit) doesn't take an
        # exclusive lock that blocks readers — the landing/opds read paths must
        # stay responsive under a traffic spike. busy_timeout keeps the rare
        # writer-vs-writer contention from erroring out immediately.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------- users

    def create_user(self, readwise_token: str, stripe_ref: str | None = None) -> str:
        encrypted = self._fernet.encrypt(readwise_token.encode()).decode()
        for _ in range(10):
            secret = generate_secret()
            try:
                with self._conn() as conn:
                    conn.execute(
                        "INSERT INTO users (secret, readwise_token, stripe_ref, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (secret, encrypted, stripe_ref, time.time()),
                    )
                return secret
            except sqlite3.IntegrityError as e:
                if "stripe_ref" in str(e):
                    raise ValueError("This payment has already been used to sign up") from e
                continue  # secret collision, retry
        raise RuntimeError("Could not generate a unique secret")

    def get_token(self, secret: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT readwise_token FROM users WHERE secret = ?", (secret,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row["readwise_token"].encode()).decode()
        except InvalidToken:
            logger.warning("Stored token for a user is undecryptable (key changed?)")
            return None

    def user_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def stripe_ref_used(self, stripe_ref: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE stripe_ref = ?", (stripe_ref,)
            ).fetchone()
        return row is not None

    def regenerate_secret(self, old_secret: str) -> str | None:
        """Give an existing user a fresh secret. Returns the new secret, or None if unknown."""
        for _ in range(10):
            new_secret = generate_secret()
            with self._conn() as conn:
                try:
                    cur = conn.execute(
                        "UPDATE users SET secret = ? WHERE secret = ?",
                        (new_secret, old_secret),
                    )
                except sqlite3.IntegrityError:
                    continue
                if cur.rowcount == 0:
                    return None
                return new_secret
        raise RuntimeError("Could not generate a unique secret")

    def delete_user(self, secret: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE secret = ?", (secret,))
        return cur.rowcount > 0

    # ------------------------------------------------------- rate-limit events
    # Durable (survives machine stop/start) and shared across instances,
    # unlike an in-process counter — see docs/review-2026-07-31.md finding 4.
    # `bucket` namespaces the counters (unknown-secret probes vs. signups) so
    # they don't spend each other's budget.

    def try_record_event(self, bucket: str, ip: str, limit: int, window: float) -> bool:
        """Count and record in one transaction. True if the event was admitted.

        This has to be atomic, not a count() followed by an insert. The whole
        reason these counters live in SQLite is that they're shared between
        instances, and separate statements let every instance read the same
        under-limit count before any of them writes — which lets a burst of
        concurrent requests past the limit entirely, not just by one or two.

        BEGIN IMMEDIATE takes the write lock up front, so the count and the
        insert can't be interleaved by another connection. busy_timeout (set in
        _conn) covers the resulting contention.
        """
        now = time.time()
        conn = self._conn()
        try:
            conn.isolation_level = None  # drive the transaction explicitly
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Prune this bucket's expired rows: windows differ per bucket,
                # so a short-window bucket must not evict a long-window one's.
                conn.execute(
                    "DELETE FROM rate_events WHERE bucket = ? AND ts < ?",
                    (bucket, now - window),
                )
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM rate_events"
                    " WHERE bucket = ? AND ip = ? AND ts >= ?",
                    (bucket, ip, now - window),
                ).fetchone()
                if row["n"] >= limit:
                    conn.execute("COMMIT")
                    return False
                conn.execute(
                    "INSERT INTO rate_events (bucket, ip, ts) VALUES (?, ?, ?)",
                    (bucket, ip, now),
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def prune_rate_events(self, max_age: float) -> int:
        """Drop rate-limit rows older than `max_age`, across every bucket.

        Pruning otherwise only happens when a later event lands in the same
        bucket, so an address that made one request and never came back would
        sit in the table indefinitely. These rows are IP addresses, and they
        have no purpose once their window has passed. Called at startup, which
        bounds how long a since-idle instance can hold them.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM rate_events WHERE ts < ?", (time.time() - max_age,)
            )
        return cur.rowcount

    def event_count(self, bucket: str, ip: str, window: float) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM rate_events"
                " WHERE bucket = ? AND ip = ? AND ts >= ?",
                (bucket, ip, time.time() - window),
            ).fetchone()
        return row["n"]

    # ------------------------------------------------- landing referrer log
    # Opt-in (only when STATS_TOKEN is set): a server-side log to attribute a
    # launch to its source. Referer + user-agent + timestamp only — no IPs, no
    # cookies. Durable on the SQLite volume, so it survives scale-to-zero.

    def record_hit(
        self,
        path: str,
        referer: str | None,
        user_agent: str | None,
        retention_days: int = 0,
    ) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO hits (ts, path, referer, user_agent) VALUES (?, ?, ?, ?)",
                (now, path, _clean_referer(referer), user_agent),
            )
            # Prune on write (like record_miss) so the log is self-limiting and
            # the "old data goes away" claim is real, not just displayed.
            if retention_days > 0:
                conn.execute(
                    "DELETE FROM hits WHERE ts < ?", (now - retention_days * 86400,)
                )

    def hit_count(self, since: float = 0.0) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM hits WHERE ts >= ?", (since,)
            ).fetchone()
        return row["n"]

    def top_referrers(self, since: float = 0.0, limit: int = 100) -> list[tuple[str, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(referer, ''), '(direct)') AS ref, COUNT(*) AS n "
                "FROM hits WHERE ts >= ? GROUP BY ref ORDER BY n DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [(r["ref"], r["n"]) for r in rows]

    def recent_hits(self, limit: int = 50) -> list[tuple[float, str, str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, path, referer, user_agent FROM hits ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["ts"], r["path"], r["referer"], r["user_agent"]) for r in rows]
