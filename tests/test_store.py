import re
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from later_ink.store import Store, generate_secret

SECRET_RE = re.compile(r"^[a-z]{3,7}(-[a-z]{3,7}){3}$")


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "test.db"), Fernet(Fernet.generate_key()))


def test_secret_format():
    for _ in range(50):
        assert SECRET_RE.match(generate_secret())


def test_create_and_get(store):
    secret = store.create_user("token-abc")
    assert SECRET_RE.match(secret)
    assert store.get_token(secret) == "token-abc"


def test_token_encrypted_at_rest(store):
    secret = store.create_user("token-abc")
    with sqlite3.connect(store.path) as conn:
        raw = conn.execute(
            "SELECT readwise_token FROM users WHERE secret = ?", (secret,)
        ).fetchone()[0]
    assert "token-abc" not in raw


def test_wrong_key_yields_none(tmp_path):
    path = str(tmp_path / "test.db")
    secret = Store(path, Fernet(Fernet.generate_key())).create_user("token-abc")
    other = Store(path, Fernet(Fernet.generate_key()))
    assert other.get_token(secret) is None


def test_unknown_secret(store):
    assert store.get_token("nope-nope-nope-nope") is None


def test_regenerate(store):
    secret = store.create_user("token-abc")
    new_secret = store.regenerate_secret(secret)
    assert new_secret != secret
    assert store.get_token(secret) is None
    assert store.get_token(new_secret) == "token-abc"
    assert store.regenerate_secret("unknown-unknown-unknown-unknown") is None


def test_delete(store):
    secret = store.create_user("token-abc")
    assert store.delete_user(secret) is True
    assert store.get_token(secret) is None
    assert store.delete_user(secret) is False


def test_stripe_ref_reuse(store):
    store.create_user("token-1", stripe_ref="cs_test_123")
    assert store.stripe_ref_used("cs_test_123") is True
    assert store.stripe_ref_used("cs_test_456") is False
    with pytest.raises(ValueError):
        store.create_user("token-2", stripe_ref="cs_test_123")


# A limit high enough that admission always succeeds, for tests about counting
# rather than about the limit itself.
NO_LIMIT = 10_000


def test_rate_event_counter(store):
    assert store.event_count("miss", "1.2.3.4", 3600) == 0
    for _ in range(5):
        assert store.try_record_event("miss", "1.2.3.4", NO_LIMIT, 3600) is True
    assert store.event_count("miss", "1.2.3.4", 3600) == 5
    assert store.event_count("miss", "5.6.7.8", 3600) == 0


def test_rate_event_buckets_are_independent(store):
    # Unknown-secret probes and signups must not spend each other's budget.
    for _ in range(3):
        store.try_record_event("miss", "1.2.3.4", NO_LIMIT, 3600)
    store.try_record_event("signup", "1.2.3.4", NO_LIMIT, 3600)
    assert store.event_count("miss", "1.2.3.4", 3600) == 3
    assert store.event_count("signup", "1.2.3.4", 3600) == 1


def test_rate_event_limit_is_enforced_per_bucket(store):
    assert store.try_record_event("signup", "1.2.3.4", 2, 3600) is True
    assert store.try_record_event("signup", "1.2.3.4", 2, 3600) is True
    assert store.try_record_event("signup", "1.2.3.4", 2, 3600) is False
    # A different bucket has its own budget, even for the same address.
    assert store.try_record_event("miss", "1.2.3.4", 2, 3600) is True


def test_rate_event_queries_are_indexed(store):
    """Both rate-limit queries must use an index, not scan.

    They run inside the BEGIN IMMEDIATE that every admission holds, and the
    row count grows with the number of distinct addresses in the window — so a
    scan here degrades fastest under exactly the probing traffic the limiter
    exists to throttle.
    """
    with sqlite3.connect(store.path) as conn:
        expiry = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM rate_events WHERE bucket = ? AND ts < ?",
            ("miss", 0.0),
        ).fetchall()[0][-1]
        count = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM rate_events"
            " WHERE bucket = ? AND ip = ? AND ts >= ?",
            ("miss", "1.2.3.4", 0.0),
        ).fetchall()[0][-1]
    # ts must be part of the expiry lookup, not just bucket: "(bucket=?)" alone
    # means every row in the bucket is walked.
    assert "rate_events_expiry (bucket=? AND ts<?)" in expiry, expiry
    assert "rate_events_lookup" in count and "ts>" in count, count


def test_pruning_one_bucket_leaves_another_intact(store):
    # Buckets have different windows; pruning the short one must not evict
    # rows the long one still counts.
    store.try_record_event("signup", "1.2.3.4", NO_LIMIT, 3600)
    store.try_record_event("miss", "1.2.3.4", NO_LIMIT, 0.0)  # prunes its own expired rows
    assert store.event_count("signup", "1.2.3.4", 3600) == 1


def test_conn_uses_wal(tmp_path):
    # A separate connection sees WAL because the mode persists in the db header,
    # so writers don't take an exclusive lock that blocks readers.
    store = Store(str(tmp_path / "wal.db"), Fernet(Fernet.generate_key()))
    with sqlite3.connect(store.path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_record_hit_strips_query_and_fragment(store):
    store.record_hit("/", "https://ref.example/r/koreader?utm=abc#frag", "ua")
    refs = dict(store.top_referrers())
    assert "https://ref.example/r/koreader" in refs  # path kept for attribution
    assert not any("utm=abc" in r or "frag" in r for r in refs)  # query/fragment gone


@pytest.mark.parametrize(
    "referer",
    [
        "http://192.168.1.5/admin?x=1",  # IPv4 literal
        "http://[2001:db8::1]/path",  # IPv6 literal
        "https://127.0.0.1:8000/",  # IPv4 + port
    ],
)
def test_record_hit_drops_ip_literal_referer(store, referer):
    # "No IPs" is a product claim — a referer with a bare-IP host must not persist.
    store.record_hit("/", referer, "ua")
    refs = dict(store.top_referrers())
    assert refs == {"(direct)": 1}  # stored as null -> counted as direct, no address kept


def test_record_hit_retention_prunes_old_rows(tmp_path):
    store = Store(str(tmp_path / "hits.db"), Fernet(Fernet.generate_key()))
    # An old hit inserted directly, then a fresh write with a 90-day window.
    old_ts = time.time() - 200 * 86400
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO hits (ts, path, referer, user_agent) VALUES (?, ?, ?, ?)",
            (old_ts, "/", "https://old.example/", "ua"),
        )
    store.record_hit("/", "https://new.example/", "ua", retention_days=90)
    assert store.hit_count() == 1  # only the fresh hit survives the prune


def test_record_hit_retention_zero_keeps_everything(tmp_path):
    store = Store(str(tmp_path / "hits2.db"), Fernet(Fernet.generate_key()))
    old_ts = time.time() - 200 * 86400
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO hits (ts, path, referer, user_agent) VALUES (?, ?, ?, ?)",
            (old_ts, "/", "https://old.example/", "ua"),
        )
    store.record_hit("/", "https://new.example/", "ua", retention_days=0)
    assert store.hit_count() == 2  # 0 = keep everything, no prune
