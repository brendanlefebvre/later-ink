import logging
import os
import secrets as pysecrets
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

from .words import WORDS

logger = logging.getLogger(__name__)

RESERVED_PATHS = {
    "opds", "start", "health", "static", "assets", "docs", "openapi.json",
    "favicon.ico", "robots.txt",
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
                CREATE TABLE IF NOT EXISTS misses (
                    ip TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS misses_ip_ts ON misses (ip, ts)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
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

    # ------------------------------------------------- unknown-secret misses
    # Durable (survives machine stop/start) and shared across instances,
    # unlike an in-process counter — see docs/review-2026-07-31.md finding 4.

    def record_miss(self, ip: str, window: float) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute("DELETE FROM misses WHERE ts < ?", (now - window,))
            conn.execute("INSERT INTO misses (ip, ts) VALUES (?, ?)", (ip, now))

    def miss_count(self, ip: str, window: float) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM misses WHERE ip = ? AND ts >= ?",
                (ip, time.time() - window),
            ).fetchone()
        return row["n"]
