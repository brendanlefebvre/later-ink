import asyncio
from datetime import datetime

import httpx
import pytest

from later_ink import config
from later_ink.connectors.base import ArticleUnavailable, UpstreamError
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


def _token_status_conn(status: int):
    """A connector whose token endpoint always answers `status`."""
    def h(request):
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=ENTRIES_PAGE)

    return _make_conn(h)


def _login_error(status: int) -> UpstreamError:
    conn = _token_status_conn(status)
    with pytest.raises(UpstreamError) as exc:
        _run(conn, lambda: conn.list_articles("unread"))
    return exc.value


def test_rate_limited_login_is_not_reported_as_bad_credentials():
    err = _login_error(429)
    assert err.status == 429
    assert "rate-limiting" in str(err)
    assert "rejected" not in str(err)


@pytest.mark.parametrize("status", [500, 503])
def test_failing_wallabag_login_is_not_reported_as_bad_credentials(status):
    err = _login_error(status)
    assert err.status == status
    assert "rejected" not in str(err)


@pytest.mark.parametrize("status", [400, 401])
def test_refused_login_still_reads_as_rejected_credentials(status):
    # OAuth2 answers a wrong password with 400 invalid_grant. _ensure_token also
    # relies on these two statuses to decide a refresh token is dead and fall
    # back to the password grant, so they must keep their status.
    err = _login_error(status)
    assert err.status == status
    assert "rejected the credentials" in str(err)


def test_transient_failure_on_refresh_is_raised_not_retried_as_password_login():
    grants = []

    def h(request):
        if request.url.path == "/oauth/v2/token":
            grant = dict(httpx.QueryParams(request.content.decode()))["grant_type"]
            grants.append(grant)
            if grant == "password":
                # expires_in 0 makes the next request refresh straight away.
                return httpx.Response(
                    200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 0}
                )
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=ENTRIES_PAGE)

    conn = _make_conn(h)

    async def go():
        try:
            await conn.list_articles("unread")
            with pytest.raises(UpstreamError) as exc:
                await conn.list_articles("unread")
            return exc.value
        finally:
            await conn.close()

    err = asyncio.run(go())
    assert err.status == 429 and "rate-limiting" in str(err)
    assert grants == ["password", "refresh_token"]  # no second password login


def test_lapsed_subscription_redirect_is_reported_with_its_target():
    # wallabag.it answers every API call on a lapsed subscription with a 302
    # to /subscription/ and an HTML body; the token endpoint keeps working.
    # The reader sees a 502 whose text names the redirect, not "unexpected
    # response" from failing to parse the HTML as JSON.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(
            302,
            headers={"Location": "/subscription/", "Content-Type": "application/json"},
            text="<!DOCTYPE html><html><body>Redirecting to /subscription/</body></html>",
        )

    conn = _make_conn(handler)
    with pytest.raises(UpstreamError) as exc:
        _run(conn, lambda: conn.list_articles("unread"))
    assert "redirected to /subscription/" in str(exc.value)
    assert "unexpected" not in str(exc.value)
