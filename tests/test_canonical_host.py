"""Host canonicalization: www.<canonical> must 301 to the bare apex.

Serving both the apex and www independently splits SEO across two hostnames
and fills the referrer log with self-referrals. The scheme redirect
(http->https) is already handled by Fly's force_https, but Fly does not do
host->host redirects, so the www->apex hop has to happen here.
"""

import pytest
from fastapi.testclient import TestClient

from later_ink import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # No ALLOW_FREE_SIGNUP / STRIPE_SECRET_KEY, so signups_enabled() is False
    # and lifespan does not demand an ENCRYPTION_KEY.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("BASE_URL", "https://later.ink")
    with TestClient(app=main.app) as c:
        yield c


def _get(client, path, host, **kwargs):
    # follow_redirects=False: TestClient chases redirects by default, which
    # would hide the 301 this whole module is about.
    return client.get(path, headers={"host": host}, follow_redirects=False, **kwargs)


def test_www_host_redirects_permanently_to_apex(client):
    resp = _get(client, "/", "www.later.ink")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://later.ink/"


def test_redirect_preserves_path_and_query(client):
    resp = _get(client, "/catalog?x=1", "www.later.ink")
    assert resp.headers["location"] == "https://later.ink/catalog?x=1"


def test_redirect_omits_question_mark_when_there_is_no_query(client):
    resp = _get(client, "/catalog", "www.later.ink")
    assert resp.headers["location"] == "https://later.ink/catalog"


def test_redirect_preserves_percent_encoded_path(client):
    """request.url.path is decoded; a naive rebuild would emit a raw space."""
    resp = _get(client, "/a%20b", "www.later.ink")
    assert resp.headers["location"] == "https://later.ink/a%20b"


def test_apex_host_is_never_redirected(client):
    """The loop guard. Only www->apex; the apex must still serve normally."""
    resp = _get(client, "/", "later.ink")
    assert resp.status_code == 200


def test_redirect_target_is_not_itself_redirected(client):
    """Follow the Location once: it must terminate, not bounce again."""
    first = _get(client, "/catalog?x=1", "www.later.ink")
    assert first.headers["location"] == "https://later.ink/catalog?x=1"
    second = _get(client, "/catalog?x=1", "later.ink")
    assert second.status_code != 301


def test_host_match_is_case_insensitive(client):
    resp = _get(client, "/", "WWW.Later.Ink")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://later.ink/"


def test_host_with_port_still_matches(client):
    resp = _get(client, "/", "www.later.ink:8000")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://later.ink/"


def test_redirect_precedes_routing(client):
    """A www request must 301 even for a path that would otherwise 404."""
    resp = _get(client, "/no-such-path", "www.later.ink")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://later.ink/no-such-path"


def test_canonical_host_follows_base_url(client, monkeypatch):
    """Not hardcoded to later.ink: a self-hoster gets the same behavior."""
    monkeypatch.setenv("BASE_URL", "https://example.org")
    resp = _get(client, "/", "www.example.org")
    assert resp.status_code == 301
    assert resp.headers["location"] == "https://example.org/"


def test_unrelated_host_is_not_redirected(client):
    resp = _get(client, "/", "later.ink.example.com")
    assert resp.status_code == 200


def test_redirect_carries_security_headers(client):
    """The 301 goes through security_headers, so www responses are hardened too."""
    resp = _get(client, "/", "www.later.ink")
    assert resp.status_code == 301
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
