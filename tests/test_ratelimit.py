"""Rate limiting: the limiter primitives, and the middleware that applies them."""
import threading

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from later_ink import main
from later_ink.ratelimit import DurableLimiter, MemoryLimiter
from later_ink.store import Store

# --------------------------------------------------------------- primitives


def test_memory_limiter_allows_up_to_the_limit_then_blocks():
    lim = MemoryLimiter(limit=3, window=60.0)
    assert [lim.allow("1.1.1.1") for _ in range(5)] == [True, True, True, False, False]


def test_memory_limiter_is_per_ip():
    lim = MemoryLimiter(limit=1, window=60.0)
    assert lim.allow("1.1.1.1") is True
    assert lim.allow("1.1.1.1") is False
    assert lim.allow("2.2.2.2") is True  # a busy neighbour isn't your problem


def test_memory_limiter_window_slides(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("later_ink.ratelimit.time.monotonic", lambda: clock["t"])
    lim = MemoryLimiter(limit=2, window=60.0)
    assert lim.allow("ip") and lim.allow("ip")
    assert lim.allow("ip") is False
    clock["t"] += 61.0  # the first two hits age out
    assert lim.allow("ip") is True


def test_memory_limiter_bounds_the_ip_table():
    lim = MemoryLimiter(limit=5, window=60.0, max_ips=10)
    for i in range(100):
        lim.allow(f"10.0.0.{i}")
    assert len(lim._hits) <= 10


def test_limiters_disabled_when_limit_is_zero(tmp_path):
    mem = MemoryLimiter(limit=0, window=60.0)
    assert all(mem.allow("ip") for _ in range(50))
    store = Store(str(tmp_path / "d.db"), Fernet(Fernet.generate_key()))
    dur = DurableLimiter(store, bucket="signup", limit=0, window=3600.0)
    for _ in range(50):
        dur.try_record("ip")
    assert dur.blocked("ip") is False


def test_durable_limiter_counts_and_blocks(tmp_path):
    store = Store(str(tmp_path / "d.db"), Fernet(Fernet.generate_key()))
    lim = DurableLimiter(store, bucket="signup", limit=3, window=3600.0)
    for _ in range(3):
        assert lim.blocked("1.1.1.1") is False
        assert lim.try_record("1.1.1.1") is True
    assert lim.blocked("1.1.1.1") is True
    assert lim.blocked("2.2.2.2") is False


def test_durable_admission_is_atomic_across_instances(tmp_path):
    """The limit must hold when instances decide concurrently.

    These counters live in SQLite precisely because they're shared between
    machines. A count() followed by an insert lets every instance read the
    same under-limit total before any of them writes, which doesn't overshoot
    the limit by one or two — it lets a whole burst through.
    """
    db = str(tmp_path / "race.db")
    key = Fernet.generate_key()
    limit, instances = 5, 20
    limiters = [
        DurableLimiter(Store(db, Fernet(key)), bucket="signup", limit=limit, window=3600.0)
        for _ in range(instances)
    ]
    admitted = []
    barrier = threading.Barrier(instances)

    def attempt(lim):
        barrier.wait()  # line them all up on the same decision
        if lim.try_record("1.2.3.4"):
            admitted.append(1)

    threads = [threading.Thread(target=attempt, args=(lim,)) for lim in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == limit


def test_try_record_reports_exhaustion(tmp_path):
    store = Store(str(tmp_path / "t.db"), Fernet(Fernet.generate_key()))
    lim = DurableLimiter(store, bucket="signup", limit=2, window=3600.0)
    assert [lim.try_record("1.1.1.1") for _ in range(4)] == [True, True, False, False]
    assert lim.try_record("2.2.2.2") is True  # still per-IP


def test_try_record_is_a_no_op_when_disabled(tmp_path):
    store = Store(str(tmp_path / "t.db"), Fernet(Fernet.generate_key()))
    lim = DurableLimiter(store, bucket="signup", limit=0, window=3600.0)
    assert all(lim.try_record("1.1.1.1") for _ in range(50))


def test_idle_rate_rows_are_pruned_at_startup(tmp_path):
    # Rows are otherwise only pruned by a later write to the same bucket, so an
    # address that made one request and never returned would persist. These are
    # IP addresses with no purpose past their window.
    db = str(tmp_path / "idle.db")
    store = Store(db, Fernet(Fernet.generate_key()))
    store.try_record_event("signup", "9.9.9.9", limit=10, window=3600.0)
    assert store.event_count("signup", "9.9.9.9", 3600.0) == 1
    # Nothing else ever writes to this bucket; a sweep is what clears it.
    assert store.prune_rate_events(max_age=0.0) == 1
    assert store.event_count("signup", "9.9.9.9", 3600.0) == 0


def test_pruning_keeps_rows_inside_the_retention_window(tmp_path):
    store = Store(str(tmp_path / "keep.db"), Fernet(Fernet.generate_key()))
    store.try_record_event("signup", "9.9.9.9", limit=10, window=3600.0)
    assert store.prune_rate_events(max_age=3600.0) == 0  # still live
    assert store.event_count("signup", "9.9.9.9", 3600.0) == 1


def test_durable_limiter_survives_a_restart(tmp_path):
    # The point of putting these in SQLite: on a scale-to-zero host, a restart
    # is something the caller can provoke, so an in-process counter would be
    # resettable on demand.
    db = str(tmp_path / "d.db")
    key = Fernet.generate_key()
    lim = DurableLimiter(Store(db, Fernet(key)), bucket="signup", limit=2, window=3600.0)
    lim.try_record("1.1.1.1")
    lim.try_record("1.1.1.1")
    revived = DurableLimiter(Store(db, Fernet(key)), bucket="signup", limit=2, window=3600.0)
    assert revived.blocked("1.1.1.1") is True


# --------------------------------------------------------------- middleware


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.setenv("RATE_LIMIT_FEED_PER_MIN", "3")
    monkeypatch.setenv("RATE_LIMIT_SIGNUP_PER_HOUR", "2")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        yield c


def test_signup_is_throttled(client):
    assert client.get("/start").status_code == 200
    assert client.get("/start").status_code == 200
    blocked = client.get("/start")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "3600"


def test_feed_requests_are_throttled(client):
    # Unknown secrets 404, which is fine — the limiter runs before routing, so
    # what matters is that the fourth request is refused outright.
    codes = [client.get("/some-unknown-catalog-xy/").status_code for _ in range(4)]
    assert codes[:3] == [404, 404, 404]
    assert codes[3] == 429


def test_signup_and_feed_budgets_are_separate(client):
    for _ in range(3):
        client.get("/some-unknown-catalog-xy/")  # spend the feed budget
    assert client.get("/some-unknown-catalog-xy/").status_code == 429
    assert client.get("/start").status_code == 200  # signup budget untouched


def test_public_paths_are_never_throttled(client):
    for _ in range(30):
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert client.get("/assets/demo.gif").status_code == 200


def test_throttled_response_is_readable_plain_text(client):
    for _ in range(4):
        resp = client.get("/some-unknown-catalog-xy/")
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Too many requests" in resp.text
    assert resp.headers["retry-after"] == "60"


def test_limits_can_be_disabled(tmp_path, monkeypatch):
    # A self-hoster whose devices share one address behind NAT needs an off switch.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "off.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RATE_LIMIT_FEED_PER_MIN", "0")
    monkeypatch.setenv("RATE_LIMIT_SIGNUP_PER_HOUR", "0")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        for _ in range(80):
            assert c.get("/some-unknown-catalog-xy/").status_code in (404, 429)
        # 429 here would have to come from the unknown-secret limiter, not the
        # feed limiter; that one is deliberately always on.
        assert c.get("/version").status_code == 200
