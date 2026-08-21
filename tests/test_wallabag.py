import asyncio
from datetime import datetime

import httpx
import pytest

from later_ink import config
from later_ink.connectors.base import ArticleUnavailable
from later_ink.connectors.wallabag import WallabagConnector, _article_from_entry

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
    return WallabagConnector(
        url="https://wb.example.com",
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pass",
        client=httpx.AsyncClient(
            base_url="https://wb.example.com",
            transport=httpx.MockTransport(handler),
        ),
    )


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


def test_refresh_token_grant_used_when_available():
    reqs: list = []

    def h(request):
        reqs.append(request)
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})
        if request.url.path == "/api/entries.json":
            return httpx.Response(200, json=ENTRIES_PAGE)
        return httpx.Response(404, json={})

    conn = _make_conn(h)
    conn._refresh_token = "seeded-RT"  # a cached refresh token from a prior login
    _run(conn, lambda: conn.list_articles("all"))

    token_req = next(r for r in reqs if r.url.path == "/oauth/v2/token")
    body = token_req.content.decode()
    assert "grant_type=refresh_token" in body  # refresh grant preferred over password
    assert "refresh_token=seeded-RT" in body
    assert "grant_type=password" not in body


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


def test_entry_content_date_from_created_at():
    art = _article_from_entry({"id": 1, "title": "T", "created_at": "2024-05-06T07:08:09+00:00"})
    assert art.content_date == datetime(2024, 5, 6, 7, 8, 9)
    assert art.content_date.tzinfo is None


def test_entry_content_date_is_none_without_created_at():
    assert _article_from_entry({"id": 1, "title": "T"}).content_date is None


def test_an_injected_client_is_used_instead_of_a_real_one():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600})
        return httpx.Response(200, json={"_embedded": {"items": []}, "page": 1, "pages": 1})

    async def run():
        client = httpx.AsyncClient(
            base_url="https://wb.example.com", transport=httpx.MockTransport(handler)
        )
        conn = WallabagConnector(
            url="https://wb.example.com",
            client_id="cid",
            client_secret="csec",
            username="user",
            password="pass",
            client=client,
        )
        try:
            await conn.list_articles("unread")
        finally:
            await conn.close()

    asyncio.run(run())
    assert "/api/entries.json" in seen
