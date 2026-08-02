import asyncio

import httpx
import pytest

from read_later_opds import config
from read_later_opds.connectors.base import ArticleUnavailable
from read_later_opds.connectors.wallabag import WallabagConnector

ENTRIES_PAGE = {
    "page": 1,
    "pages": 2,
    "limit": 30,
    "total": 40,
    "_embedded": {
        "items": [
            {
                "id": 7,
                "title": "Rust ownership",
                "url": "https://example.com/1",
                "content": "<p>hello</p>",
                "published_by": ["Ann", "Bob"],
                "updated_at": "2024-01-15T10:30:00+0000",
                "language": "en",
                "preview_picture": "https://example.com/img.png",
            },
            {"id": 8, "title": "Second", "content": "<p>two</p>"},
        ]
    },
}


def _make_conn(handler) -> WallabagConnector:
    conn = WallabagConnector(
        url="https://wb.example.com",
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pass",
    )
    # Swap the real HTTP client for a mocked transport, keeping the base URL.
    conn._client = httpx.AsyncClient(
        base_url="https://wb.example.com",
        transport=httpx.MockTransport(handler),
    )
    return conn


def _handler(requests: list):
    def h(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/oauth/v2/token":
            return httpx.Response(
                200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}
            )
        if path == "/api/entries.json":
            return httpx.Response(200, json=ENTRIES_PAGE)
        if path == "/api/entries/7.json":
            return httpx.Response(200, json=ENTRIES_PAGE["_embedded"]["items"][0])
        return httpx.Response(404, json={})

    return h


def _run(conn, coro_factory):
    async def run():
        try:
            return await coro_factory()
        finally:
            await conn.close()

    return asyncio.run(run())


def test_auth_then_list_articles():
    reqs: list = []
    conn = _make_conn(_handler(reqs))
    articles, cursor = _run(conn, lambda: conn.list_articles("unread"))

    assert [a.id for a in articles] == ["7", "8"]
    assert articles[0].author == "Ann, Bob"  # published_by joined
    assert articles[0].image_url == "https://example.com/img.png"
    assert articles[0].category == "article"
    assert articles[0].summary == "hello"  # excerpt derived from content
    assert cursor == "2"  # page 1 of 2 -> next page

    token_reqs = [r for r in reqs if r.url.path == "/oauth/v2/token"]
    entry_reqs = [r for r in reqs if r.url.path == "/api/entries.json"]
    assert len(token_reqs) == 1  # authenticated once
    assert entry_reqs[0].headers["authorization"] == "Bearer AT"
    assert entry_reqs[0].url.params["archive"] == "0"  # unread filter applied


@pytest.mark.parametrize(
    "folder,key,value",
    [("unread", "archive", "0"), ("starred", "starred", "1"), ("archive", "archive", "1")],
)
def test_folder_filters(folder, key, value):
    reqs: list = []
    conn = _make_conn(_handler(reqs))
    _run(conn, lambda: conn.list_articles(folder))
    entry_req = next(r for r in reqs if r.url.path == "/api/entries.json")
    assert entry_req.url.params[key] == value


def test_all_folder_sends_no_status_filter():
    reqs: list = []
    conn = _make_conn(_handler(reqs))
    _run(conn, lambda: conn.list_articles("all"))
    entry_req = next(r for r in reqs if r.url.path == "/api/entries.json")
    assert "archive" not in entry_req.url.params
    assert "starred" not in entry_req.url.params


def test_get_article_html_returns_content():
    conn = _make_conn(_handler([]))
    article, html = _run(conn, lambda: conn.get_article_html("7"))
    assert article.id == "7"
    assert "hello" in html


def test_missing_content_raises_unavailable():
    def h(request):
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(200, json={"id": 9, "title": "Empty", "content": ""})

    conn = _make_conn(h)
    with pytest.raises(ArticleUnavailable) as exc:
        _run(conn, lambda: conn.get_article_html("9"))
    assert exc.value.status == 422


def test_deleted_entry_is_readable_404():
    def h(request):
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(404, json={})

    conn = _make_conn(h)
    with pytest.raises(ArticleUnavailable) as exc:
        _run(conn, lambda: conn.get_article_html("123"))
    assert exc.value.status == 404


def test_401_triggers_reauth_and_retry():
    state = {"entries": 0}

    def h(request):
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if request.url.path == "/api/entries.json":
            state["entries"] += 1
            if state["entries"] == 1:
                return httpx.Response(401, json={})  # stale token
            return httpx.Response(200, json=ENTRIES_PAGE)
        return httpx.Response(404)

    conn = _make_conn(h)
    articles, _ = _run(conn, lambda: conn.list_articles("all"))
    assert [a.id for a in articles] == ["7", "8"]  # recovered after re-auth
    assert state["entries"] == 2  # retried once


def test_search_uses_native_endpoint():
    reqs: list = []

    def h(request):
        reqs.append(request)
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if request.url.path == "/api/search.json":
            return httpx.Response(200, json=ENTRIES_PAGE)
        return httpx.Response(404, json={})

    conn = _make_conn(h)
    articles, cursor = _run(conn, lambda: conn.search("rust"))

    assert [a.id for a in articles] == ["7", "8"]
    assert cursor == "2"
    search_reqs = [r for r in reqs if r.url.path == "/api/search.json"]
    assert search_reqs and search_reqs[0].url.params["term"] == "rust"  # native term search
    # never scanned folders when native search is available
    assert not any(r.url.path == "/api/entries.json" for r in reqs)


def test_search_falls_back_to_scan_when_endpoint_missing():
    # Older Wallabag without /api/search: the connector should fall back to the
    # inherited client-side scan over folders.
    def h(request):
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if request.url.path == "/api/search.json":
            return httpx.Response(404, json={})
        if request.url.path == "/api/entries.json":
            return httpx.Response(200, json=ENTRIES_PAGE)
        return httpx.Response(404, json={})

    conn = _make_conn(h)
    # "rust" matches item 7's title; the base scan filters on title/author/summary.
    articles, _ = _run(conn, lambda: conn.search("rust"))
    assert "7" in {a.id for a in articles}


def test_search_blank_query_returns_nothing():
    conn = _make_conn(_handler([]))
    articles, cursor = _run(conn, lambda: conn.search("   "))
    assert articles == [] and cursor is None


def test_config_requires_all_fields(monkeypatch):
    for k in (
        "WALLABAG_URL",
        "WALLABAG_CLIENT_ID",
        "WALLABAG_CLIENT_SECRET",
        "WALLABAG_USERNAME",
        "WALLABAG_PASSWORD",
    ):
        monkeypatch.delenv(k, raising=False)
    assert config.get_wallabag_config() is None

    monkeypatch.setenv("WALLABAG_URL", "https://wb.example.com/")
    monkeypatch.setenv("WALLABAG_CLIENT_ID", "cid")
    monkeypatch.setenv("WALLABAG_CLIENT_SECRET", "csec")
    monkeypatch.setenv("WALLABAG_USERNAME", "user")
    assert config.get_wallabag_config() is None  # password still missing

    monkeypatch.setenv("WALLABAG_PASSWORD", "pass")
    cfg = config.get_wallabag_config()
    assert cfg["url"] == "https://wb.example.com"  # trailing slash stripped
    assert cfg["client_id"] == "cid"
