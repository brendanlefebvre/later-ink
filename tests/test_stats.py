import sqlite3

import pytest
from fastapi.testclient import TestClient

from later_ink import main
from later_ink.store import classify_user_agent


@pytest.fixture()
def stats_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("STATS_TOKEN", "sekret")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        yield c


def test_referrer_logged_and_stats_gated(stats_client):
    c = stats_client
    c.get("/", headers={"referer": "https://news.ycombinator.com/"})
    c.get("/", headers={"referer": "https://old.reddit.com/r/koreader"})
    c.get("/", headers={"referer": "https://old.reddit.com/r/koreader"})
    c.get("/")  # no referer -> counts as (direct)

    # gate: the endpoint is invisible without the right token (404, not 403)
    assert c.get("/stats").status_code == 404
    assert c.get("/stats", params={"token": "wrong"}).status_code == 404

    page = c.get("/stats", params={"token": "sekret"})
    assert page.status_code == 200
    assert "<strong>4</strong> total hits" in page.text  # all four landing views recorded
    assert "news.ycombinator.com" in page.text
    assert "r/koreader" in page.text
    assert "(direct)" in page.text  # the referer-less hit


def test_no_ip_or_cookie_stored(stats_client):
    # The log is referer + user-agent only; hitting from a client IP must not
    # surface an address, and we set no cookies.
    resp = stats_client.get("/", headers={"referer": "https://example.com/x"})
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    page = stats_client.get("/stats", params={"token": "sekret"}).text
    assert "testclient" in page or "python" in page.lower()  # UA is shown
    assert "127.0.0.1" not in page  # but no IP


def test_landing_sets_no_referrer_policy(stats_client):
    # Catalog pages live under /{secret}/; without this, the wordmark and footer
    # links would leak the secret via Referer (to / and to GitHub).
    page = stats_client.get("/").text
    assert '<meta name="referrer" content="no-referrer">' in page


def test_referer_query_stripped_in_stats(stats_client):
    c = stats_client
    c.get("/", headers={"referer": "https://news.ycombinator.com/item?id=42&utm=x"})
    page = c.get("/stats", params={"token": "sekret"}).text
    assert "news.ycombinator.com/item" in page  # path kept
    assert "utm=x" not in page and "id=42" not in page  # query dropped


def test_stats_disabled_without_token(tmp_path, monkeypatch):
    db = tmp_path / "app2.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.delenv("STATS_TOKEN", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        c.get("/", headers={"referer": "https://example.com"})  # not recorded
        # /stats is a 404 for any token when the feature is off
        assert c.get("/stats", params={"token": "anything"}).status_code == 404
    # Disabled means no write happened, not just a hidden page.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0] == 0


def test_stats_non_ascii_token_gates_without_error(tmp_path, monkeypatch):
    # A non-ASCII STATS_TOKEN must not make hmac.compare_digest raise (500);
    # it should still gate cleanly (404 on a wrong token, 200 on the right one).
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app3.db"))
    monkeypatch.setenv("STATS_TOKEN", "clé-secrète-café")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        assert c.get("/stats", params={"token": "wrong"}).status_code == 404
        ok = c.get("/stats", params={"token": "clé-secrète-café"})
        assert ok.status_code == 200
        assert ok.headers.get("cache-control") == "private, no-store"


@pytest.mark.parametrize(
    "ua, bucket, family",
    [
        # Browsers: version noise collapses to a stable family label.
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0 Safari/605.1.15",
            "browser", "Safari (iOS)",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "browser", "Chrome",
        ),
        (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
            "browser", "Chrome (Android)",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_1) AppleWebKit/601.2.4 "
            "(KHTML, like Gecko) Version/9.0.1 Safari/601.2.4",
            "browser", "Safari (macOS)",
        ),
        (
            "Mozilla/5.0 (X11; U; Linux x86_64; en-US; rv:1.9.0.3) "
            "Gecko/2008092814 (Debian-3.0.1-1)",
            "browser", "Firefox",
        ),
        # Bots and crawlers — matched even behind a Mozilla/5.0 prefix.
        ("Mozilla/5.0 (compatible; CensysInspect/1.1; +https://about.censys.io/)",
         "other", "Censys"),
        ("Mozilla/5.0 (compatible; ReconX/1.0)", "other", "ReconX"),
        ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
         "ChatGPT-User/1.0; +https://openai.com/bot", "other", "ChatGPT-User"),
        # Native app HTTP clients: no Mozilla, keyed on the leading product.
        ("Hydra/22 CFNetwork/3860.700.1 Darwin/25.6.0", "other", "Hydra (app)"),
        ("Artemis/384 CFNetwork/3860.600.12 Darwin/25.5.0", "other", "Artemis (app)"),
        # Degenerate inputs fall through to the catch-alls.
        ("", "other", "(none)"),
        (None, "other", "(none)"),
    ],
)
def test_classify_user_agent(ua, bucket, family):
    assert classify_user_agent(ua) == (bucket, family)


def test_user_agent_breakdown_aggregates_families(stats_client):
    c = stats_client
    # Two Chrome desktop hits at different versions must fold into one family.
    c.get("/", headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/78 Safari/537.36"})
    c.get("/", headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"})
    c.get("/", headers={"user-agent": "Hydra/22 CFNetwork/3860.700.1 Darwin/25.6.0"})
    c.get("/", headers={"user-agent": "Mozilla/5.0 (compatible; CensysInspect/1.1; "
                         "+https://about.censys.io/)"})

    page = c.get("/stats", params={"token": "sekret"}).text
    assert "Top browsers" in page
    assert "Bots &amp; app clients" in page
    # Both Chrome versions counted together (2), under a single version-less row.
    assert '<td class="n">2</td><td class="ref">Chrome</td>' in page
    assert "Hydra (app)" in page
    assert "Censys" in page


def test_stats_accepts_bearer_token(stats_client):
    # A token in the query string lands in access logs and browser history;
    # the header is the option for anything that can set one.
    c = stats_client
    assert c.get("/stats", headers={"Authorization": "Bearer sekret"}).status_code == 200
    assert c.get("/stats", headers={"Authorization": "bearer sekret"}).status_code == 200
    assert c.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 404
    # A present-but-wrong header is not rescued by a correct query param.
    resp = c.get("/stats", params={"token": "sekret"}, headers={"Authorization": "Bearer no"})
    assert resp.status_code == 404
