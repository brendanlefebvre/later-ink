import os
import secrets as pysecrets
import sqlite3
import time

from .words import WORDS

RESERVED_PATHS = {
    "opds", "start", "health", "static", "docs", "openapi.json",
    "favicon.ico", "robots.txt",
}

SECRET_WORD_COUNT = 3


def generate_secret() -> str:
    return "-".join(pysecrets.choice(WORDS) for _ in range(SECRET_WORD_COUNT))


class Store:
    def __init__(self, path: str):
        self.path = path
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

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_user(self, readwise_token: str, stripe_ref: str | None = None) -> str:
        for _ in range(10):
            secret = generate_secret()
            try:
                with self._conn() as conn:
                    conn.execute(
                        "INSERT INTO users (secret, readwise_token, stripe_ref, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (secret, readwise_token, stripe_ref, time.time()),
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
        return row["readwise_token"] if row else None

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
