import pytest
from fastapi.testclient import TestClient

from read_later_opds import main


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
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app2.db"))
    monkeypatch.delenv("STATS_TOKEN", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        c.get("/", headers={"referer": "https://example.com"})  # not recorded
        # /stats is a 404 for any token when the feature is off
        assert c.get("/stats", params={"token": "anything"}).status_code == 404
