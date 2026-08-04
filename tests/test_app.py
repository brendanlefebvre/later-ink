import re

import pytest
from fastapi.testclient import TestClient

from later_ink import main
from later_ink.connectors import readwise
from later_ink.connectors.base import Article, Folder
from later_ink.connectors.readwise import ReadwiseConnector

SECRET_PAT = r"([a-z]+(?:-[a-z]+){3})"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)

    async def fake_validate(token):
        return token == "good-token"

    monkeypatch.setattr(readwise, "validate_token", fake_validate)

    async def fake_list_folders(self):
        return [Folder("later", "Later")]

    async def fake_list_articles(self, folder_id, cursor=None):
        return [Article(id="42", title="Test Article", author="Ann Author")], None

    monkeypatch.setattr(ReadwiseConnector, "list_folders", fake_list_folders)
    monkeypatch.setattr(ReadwiseConnector, "list_articles", fake_list_articles)

    with TestClient(app=main.app) as c:
        yield c


def _signup(client) -> tuple[str, str]:
    resp = client.post("/start", data={"readwise_token": "good-token"})
    assert resp.status_code == 200
    m = re.search(rf"/{SECRET_PAT}/regenerate", resp.text)
    assert m, "no catalog secret in success page"
    secret = m.group(1)
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', resp.text)
    assert m, "no csrf token in success page"
    return secret, m.group(1)


def test_landing(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "OPDS" in resp.text


def test_health(client):
    resp = client.get("/health")
    assert resp.json()["signup"] == "free"


def test_healthz_liveness(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}  # minimal, leaks no config
    assert client.head("/healthz").status_code == 200


def test_version_endpoint(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    version = resp.json()["version"]
    assert isinstance(version, str) and version  # non-empty release string


def test_signup_bad_token(client):
    resp = client.post("/start", data={"readwise_token": "bad-token"})
    assert resp.status_code == 400
    assert "rejected" in resp.text


def test_signup_and_browse(client):
    secret, _ = _signup(client)

    resp = client.get(f"/{secret}/")
    assert resp.status_code == 200
    assert "kind=navigation" in resp.headers["content-type"]
    assert "<title>Later.Ink</title>" in resp.text

    resp = client.get(f"/{secret}/later/")
    assert resp.status_code == 200
    assert "Test Article" in resp.text
    assert f"/{secret}/articles/42.epub" in resp.text


def test_unknown_secret_404(client):
    resp = client.get("/maple-crater-nothing-here/")
    assert resp.status_code == 404


def test_reserved_path_not_a_secret(client):
    resp = client.get("/start")
    assert resp.status_code == 200  # onboarding page, not a 404-catalog probe


def test_rate_limit_unknown_secrets(client):
    for _ in range(20):
        resp = client.get("/guess-guess-guess-guess/")
        assert resp.status_code == 404
    resp = client.get("/guess-guess-guess-guess/")
    assert resp.status_code == 429


def test_csrf_required(client):
    secret, _ = _signup(client)
    assert client.post(f"/{secret}/regenerate").status_code == 403
    assert client.post(f"/{secret}/delete", data={"csrf": "wrong"}).status_code == 403
    assert client.get(f"/{secret}/").status_code == 200  # still alive


def test_regenerate_and_delete(client):
    secret, csrf = _signup(client)

    resp = client.post(f"/{secret}/regenerate", data={"csrf": csrf})
    assert resp.status_code == 200
    m = re.search(rf"/{SECRET_PAT}/regenerate", resp.text)
    new_secret = m.group(1)
    assert new_secret != secret
    new_csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', resp.text).group(1)

    assert client.get(f"/{secret}/").status_code == 404
    assert client.get(f"/{new_secret}/").status_code == 200

    resp = client.post(f"/{new_secret}/delete", data={"csrf": new_csrf})
    assert resp.status_code == 200
    assert client.get(f"/{new_secret}/").status_code == 404


def test_upstream_error_becomes_502(client):
    from later_ink.connectors.base import UpstreamError

    async def failing_list_articles(self, folder_id, cursor=None):
        raise UpstreamError("Readwise is rate-limiting this account", 429)

    secret, _ = _signup(client)
    orig = ReadwiseConnector.list_articles
    ReadwiseConnector.list_articles = failing_list_articles
    try:
        resp = client.get(f"/{secret}/later/")
    finally:
        ReadwiseConnector.list_articles = orig
    assert resp.status_code == 502
    assert "rate-limiting" in resp.text


def test_article_unavailable_is_readable_not_500(client):
    from later_ink.connectors.base import ArticleUnavailable

    async def no_content(self, article_id):
        raise ArticleUnavailable("no readable article text here", status=422)

    secret, _ = _signup(client)
    orig = ReadwiseConnector.get_article_html
    ReadwiseConnector.get_article_html = no_content
    try:
        resp = client.get(f"/{secret}/articles/999.epub")
    finally:
        ReadwiseConnector.get_article_html = orig
    assert resp.status_code == 422
    assert "readable article text" in resp.text


def test_single_user_root_flattens_to_folders(client):
    # With exactly one connector, /opds/ should show its folders directly
    # rather than a one-item "Readwise Reader" chooser.
    main._connectors["readwise"] = ReadwiseConnector("good-token")
    try:
        resp = client.get("/opds/")
        assert resp.status_code == 200
        assert "/opds/readwise/later/" in resp.text  # folder link, not a connector link
        assert "<title>Later</title>" in resp.text
    finally:
        main._connectors.clear()


def test_catalog_advertises_search(client):
    secret, _ = _signup(client)
    resp = client.get(f"/{secret}/")
    assert 'rel="search"' in resp.text
    assert f"/{secret}/search.xml" in resp.text


def test_search_description_document(client):
    secret, _ = _signup(client)
    resp = client.get(f"/{secret}/search.xml")
    assert resp.status_code == 200
    assert "opensearchdescription" in resp.headers["content-type"]
    assert "OpenSearchDescription" in resp.text
    assert f"/{secret}/search?q={{searchTerms}}" in resp.text  # placeholder preserved


def test_search_returns_matching_articles(client):
    secret, _ = _signup(client)
    resp = client.get(f"/{secret}/search", params={"q": "test"})
    assert resp.status_code == 200
    assert "kind=acquisition" in resp.headers["content-type"]
    assert "Test Article" in resp.text
    assert f"/{secret}/articles/42.epub" in resp.text  # downloadable result


def test_search_no_match_returns_empty_feed(client):
    secret, _ = _signup(client)
    resp = client.get(f"/{secret}/search", params={"q": "nonexistent-term"})
    assert resp.status_code == 200
    assert "Test Article" not in resp.text


def test_self_host_search(client):
    main._connectors["readwise"] = ReadwiseConnector("good-token")
    try:
        root = client.get("/opds/")
        assert 'rel="search"' in root.text and "/opds/readwise/search.xml" in root.text

        desc = client.get("/opds/readwise/search.xml")
        assert "{searchTerms}" in desc.text

        hits = client.get("/opds/readwise/search", params={"q": "test"})
        assert hits.status_code == 200
        assert "Test Article" in hits.text
    finally:
        main._connectors.clear()


def test_head_requests_supported_on_feeds(client):
    secret, _ = _signup(client)
    assert client.head("/opds/").status_code == 200
    assert client.head(f"/{secret}/").status_code == 200
    assert client.head(f"/{secret}/later/").status_code == 200


def test_font_asset_served_and_accepts_head(client):
    get = client.get("/assets/fonts/league-spartan.ttf")
    assert get.status_code == 200
    assert get.headers["content-type"] == "font/ttf"
    assert client.head("/assets/fonts/league-spartan.ttf").status_code == 200


def test_demo_gif_served(client):
    get = client.get("/assets/demo.gif")
    assert get.status_code == 200
    assert get.headers["content-type"] == "image/gif"
    assert get.content[:6] in (b"GIF87a", b"GIF89a")
    assert client.head("/assets/demo.gif").status_code == 200


def test_single_user_mode_root(client):
    resp = client.get("/opds/")
    assert resp.status_code == 200
    assert "kind=navigation" in resp.headers["content-type"]


def test_feed_ids_use_later_ink_urns(client):
    main._connectors["readwise"] = ReadwiseConnector("good-token")
    try:
        resp = client.get("/opds/")
        assert resp.status_code == 200
        assert "urn:later-ink:" in resp.text
        assert "read-later-opds" not in resp.text
    finally:
        main._connectors.clear()
