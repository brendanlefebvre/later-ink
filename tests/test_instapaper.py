"""Unit tests for the Instapaper API v2 connector.

Every test injects an httpx client with a MockTransport (the contract's seam)
except test_build_client_sends_bearer_token_and_v2_base_url, which drives the
client production builds. v1's live test found a bug no injected-client test
could see, so that one test is the point.
"""

import asyncio
import base64
import json
from datetime import datetime

import httpx
import pytest

from later_ink.connectors.base import ArticleUnavailable, UpstreamError
from later_ink.connectors.instapaper import (
    BASE_URL,
    PAGE_SIZE,
    InstapaperConnector,
    _decode_article_id,
    _encode_article_id,
    build_client,
)

BASE = "https://instapaper.test/api/2"
TIME = 1735779845  # 2025-01-02T01:04:05Z
BOOKMARK = {
    "id": 42,
    "url": "https://example.com/a",
    "title": "Some Title",
    "description": "An excerpt",
    "image": "https://example.com/a.jpg",
    "progress": {"percentage": 0.0, "timestamp": 0},
    "liked": False,
    "archived": False,
    "time": TIME,
    "pubtime": None,
    "author": "A. Writer",
    "folder_id": None,
    "tags": [],
    "private_source": None,
    "category": 0,
}
PARSED = {
    "metadata": {
        "title": "Some Title From The Page",
        "author": {"name": "Page Byline", "url": None},
        "pubtime": None,
        "thumbnail": None,
        "description": None,
        "private_source": None,
        "category": 0,
    },
    "content": {
        "body": "<article><p>Body</p></article>",
        "images": [],
        "words": 1,
        "paywalled": False,
        "direction": "ltr",
    },
}


def _error(status: int, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


def _page(bookmarks: list, total: int) -> httpx.Response:
    # Raw content, not json=: httpx 0.28 refuses to serialize the inf the
    # unusable-time case needs, but a real upstream could send it.
    body = json.dumps({"bookmarks": bookmarks, "total": total})
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


def _bookmarks(n: int, start: int = 1) -> list:
    return [{**BOOKMARK, "id": i, "title": f"Title {i}"} for i in range(start, start + n)]


def _conn(handler):
    client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    return InstapaperConnector("tok", client=client), client


def _run(conn, call):
    async def go():
        try:
            return await call(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def _list(handler, folder_id="home", cursor=None):
    conn, _ = _conn(handler)
    return _run(conn, lambda c: c.list_articles(folder_id, cursor))


# ---------------------------------------------------------------- article ids


def test_article_id_round_trips():
    token = _encode_article_id("42", TIME, "Some Title", "https://example.com/a")
    assert _decode_article_id(token) == ("42", TIME, "Some Title", "https://example.com/a")


def test_article_id_round_trips_empty_url():
    token = _encode_article_id("42", TIME, "Some Title", None)
    assert _decode_article_id(token) == ("42", TIME, "Some Title", "")


def test_article_id_round_trips_missing_time():
    token = _encode_article_id("42", None, "Some Title", None)
    assert _decode_article_id(token) == ("42", None, "Some Title", "")


def test_article_id_token_is_url_path_safe():
    token = _encode_article_id("42", TIME, "Café, déjà vu", "https://example.com/a?x=1")
    assert all(c.isalnum() or c in "-_" for c in token)


def test_decode_rejects_malformed_token():
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id("!!!not-base64!!!")
    assert exc.value.status == 404


def test_decode_rejects_wellformed_base64_missing_fields():
    bad = base64.urlsafe_b64encode(json.dumps({"i": "42"}).encode()).decode().rstrip("=")
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id(bad)
    assert exc.value.status == 404


# ------------------------------------------------------- production client


def test_build_client_sends_bearer_token_and_v2_base_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"folders": []})

    client = build_client("tok", transport=httpx.MockTransport(handler))
    conn = InstapaperConnector("tok", client=client)
    _run(conn, lambda c: c.list_folders())

    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == f"{BASE_URL}/folders"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["Accept"] == "application/json"


def test_default_client_is_the_production_client():
    conn = InstapaperConnector("tok")
    try:
        assert str(conn._client.base_url).rstrip("/") == BASE_URL
        assert conn._client.headers["Authorization"] == "Bearer tok"
    finally:
        asyncio.run(conn.close())


# ---------------------------------------------------------------- folders


def test_list_folders_has_builtins_then_user_folders_in_api_order():
    def handler(request):
        assert request.url.path == "/api/2/folders"
        return httpx.Response(
            200,
            json={
                "folders": [
                    {"id": 200, "title": "Travel", "slug": "travel", "position": 1, "public": False, "count": 3},
                    {"id": 100, "title": "Recipes", "slug": "recipes", "position": 2, "public": False, "count": 9},
                ]
            },
        )

    conn, _ = _conn(handler)
    folders = _run(conn, lambda c: c.list_folders())
    assert [f.id for f in folders] == ["home", "liked", "archive", "200", "100"]
    assert [f.title for f in folders[3:]] == ["Travel", "Recipes"]


def test_list_folders_tolerates_missing_title_and_odd_entries():
    def handler(request):
        return httpx.Response(200, json={"folders": [{"id": 5}, "junk", {"title": "no id"}]})

    conn, _ = _conn(handler)
    folders = _run(conn, lambda c: c.list_folders())
    assert [f.id for f in folders[3:]] == ["5"]
    assert folders[3].title == "5"


def test_list_folders_rejects_a_non_object_body():
    conn, _ = _conn(lambda request: httpx.Response(200, json=[{"id": 5}]))
    with pytest.raises(UpstreamError):
        _run(conn, lambda c: c.list_folders())


# ------------------------------------------------------- query construction


def test_builtin_folder_sends_section_and_never_since():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return _page([BOOKMARK], 1)

    _list(handler, "archive")
    assert seen == [{"section": "archive", "limit": str(PAGE_SIZE), "offset": "0"}]


def test_user_folder_sends_folder_id_without_section():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return _page([BOOKMARK], 1)

    _list(handler, "100")
    assert seen == [{"folder_id": "100", "limit": str(PAGE_SIZE), "offset": "0"}]


# ---------------------------------------------------------- article mapping


def test_list_articles_maps_bookmark_fields():
    articles, cursor = _list(lambda request: _page([BOOKMARK], 1))
    assert cursor is None
    (a,) = articles
    assert _decode_article_id(a.id) == ("42", TIME, "Some Title", "https://example.com/a")
    assert a.title == "Some Title"
    assert a.author == "A. Writer"
    assert a.summary == "An excerpt"
    assert a.url == "https://example.com/a"
    assert a.image_url == "https://example.com/a.jpg"
    assert a.category == "article"
    assert a.content_date == datetime(2025, 1, 2, 1, 4, 5)
    assert a.content_date.tzinfo is None
    assert a.word_count is None


@pytest.mark.parametrize("raw,expected", [(1, "email"), (2, "video"), (3, "pdf"), (4, "social"), (9, "article"), (None, "article"), ("x", "article")])
def test_category_maps_known_codes_and_defaults_to_article(raw, expected):
    articles, _ = _list(lambda request: _page([{**BOOKMARK, "category": raw}], 1))
    assert articles[0].category == expected


def test_private_bookmark_with_null_title_and_url_lists_as_untitled():
    bm = {**BOOKMARK, "title": None, "url": None, "private_source": "Acme Reader"}
    articles, _ = _list(lambda request: _page([bm], 1))
    (a,) = articles
    assert a.title == "Untitled"
    assert a.url is None
    assert _decode_article_id(a.id) == ("42", TIME, "Untitled", "")


@pytest.mark.parametrize("bad_time", [None, "soon", "", 1.5e400])
def test_unusable_time_gives_no_content_date(bad_time):
    articles, _ = _list(lambda request: _page([{**BOOKMARK, "time": bad_time}], 1))
    assert articles[0].content_date is None
    assert _decode_article_id(articles[0].id)[1] is None


def test_non_dict_and_idless_entries_are_skipped():
    articles, _ = _list(lambda request: _page(["junk", {"title": "no id"}, BOOKMARK], 3))
    assert [a.title for a in articles] == ["Some Title"]


def test_bookmarks_that_is_not_a_list_is_an_upstream_error():
    with pytest.raises(UpstreamError):
        _list(lambda request: httpx.Response(200, json={"bookmarks": {"id": 1}, "total": 1}))


# ------------------------------------------------------------------ paging


def test_full_page_with_more_to_come_yields_the_next_offset():
    _, cursor = _list(lambda request: _page(_bookmarks(PAGE_SIZE), 250))
    assert cursor == str(PAGE_SIZE)


def test_cursor_is_sent_as_offset_and_advances():
    seen = []

    def handler(request):
        seen.append(request.url.params["offset"])
        return _page(_bookmarks(PAGE_SIZE, start=101), 250)

    _, cursor = _list(handler, cursor="100")
    assert seen == ["100"]
    assert cursor == "200"


def test_short_page_ends_the_feed_even_if_total_says_more():
    _, cursor = _list(lambda request: _page(_bookmarks(3), 999))
    assert cursor is None


def test_full_page_that_reaches_total_ends_the_feed():
    _, cursor = _list(lambda request: _page(_bookmarks(PAGE_SIZE), PAGE_SIZE))
    assert cursor is None


@pytest.mark.parametrize("total", [None, "many", -1])
def test_full_page_with_unusable_total_continues(total):
    _, cursor = _list(lambda request: httpx.Response(200, json={"bookmarks": _bookmarks(PAGE_SIZE), "total": total}))
    assert cursor == str(PAGE_SIZE)


@pytest.mark.parametrize("cursor", ["", "abc", "-5", "1.5"])
def test_unusable_cursor_restarts_at_offset_zero(cursor):
    seen = []

    def handler(request):
        seen.append(request.url.params["offset"])
        return _page([BOOKMARK], 1)

    _list(handler, cursor=cursor)
    assert seen == ["0"]


def test_next_cursor_counts_raw_entries_not_kept_articles():
    # A page of PAGE_SIZE raw entries where some are junk is still a full page.
    raw = _bookmarks(PAGE_SIZE - 1) + ["junk"]
    articles, cursor = _list(lambda request: _page(raw, 500))
    assert len(articles) == PAGE_SIZE - 1
    assert cursor == str(PAGE_SIZE)


# ------------------------------------------------------------ status mapping


@pytest.mark.parametrize(
    "response,status",
    [
        (httpx.Response(401, text=""), 401),
        (_error(401), 401),
        (_error(402), 402),
        (_error(403), 403),
        (_error(404), 404),
        (_error(500), 500),
        (httpx.Response(500, text="<html>broken</html>"), 500),
        (httpx.Response(302, headers={"Location": "https://www.instapaper.com/login"}), 502),
    ],
)
def test_error_statuses_are_upstream_errors_with_the_status(response, status):
    with pytest.raises(UpstreamError) as exc:
        _list(lambda request: response)
    assert exc.value.status == status


def test_402_and_403_have_their_own_messages():
    with pytest.raises(UpstreamError) as exc:
        _list(lambda request: _error(402))
    assert "quota" in str(exc.value)
    with pytest.raises(UpstreamError) as exc:
        _list(lambda request: _error(403))
    assert "registered" in str(exc.value)


def test_error_message_text_is_not_surfaced_or_matched():
    with pytest.raises(UpstreamError) as exc:
        _list(lambda request: _error(500, "Bookmark not found"))
    assert "Bookmark not found" not in str(exc.value)


def test_429_is_retried_once_then_raised():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"code": 429, "message": "slow down"}})

    with pytest.raises(UpstreamError) as exc:
        _list(handler)
    assert exc.value.status == 429
    assert len(calls) == 2


def test_429_then_200_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _page([BOOKMARK], 1)

    articles, _ = _list(handler)
    assert len(articles) == 1 and len(calls) == 2


def test_non_json_200_is_an_upstream_error():
    with pytest.raises(UpstreamError):
        _list(lambda request: httpx.Response(200, text="<html>captive portal</html>"))


def test_unreachable_host_is_an_upstream_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(UpstreamError):
        _list(handler)


# ---------------------------------------------------------------- download


ARTICLE_ID = _encode_article_id("42", TIME, "Some Title", "https://example.com/a")


def _download(handler, article_id=ARTICLE_ID):
    conn, _ = _conn(handler)
    return _run(conn, lambda c: c.get_article_html(article_id))


def test_download_calls_parse_with_no_parameters():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=PARSED)

    _download(handler)
    assert seen == [("GET", "/api/2/bookmarks/42/parse", {})]


def test_download_returns_body_and_metadata_from_the_id_not_the_parser():
    article, html = _download(lambda request: httpx.Response(200, json=PARSED))
    assert html == "<article><p>Body</p></article>"
    assert article.id == ARTICLE_ID
    assert article.title == "Some Title"  # the bookmark's title, not "Some Title From The Page"
    assert article.url == "https://example.com/a"
    assert article.author == "Page Byline"
    assert article.content_date == datetime(2025, 1, 2, 1, 4, 5)
    assert article.content_date.tzinfo is None


def test_download_with_empty_url_in_id_has_no_url():
    article, _ = _download(lambda request: httpx.Response(200, json=PARSED), _encode_article_id("42", TIME, "T", None))
    assert article.url is None


def test_download_without_parser_author_has_none():
    payload = {**PARSED, "metadata": {**PARSED["metadata"], "author": None}}
    article, _ = _download(lambda request: httpx.Response(200, json=payload))
    assert article.author is None


def test_parse_400_is_an_unavailable_article_422():
    with pytest.raises(ArticleUnavailable) as exc:
        _download(lambda request: _error(400, "Bookmark not found"))
    assert exc.value.status == 422


@pytest.mark.parametrize(
    "payload",
    [
        {**PARSED, "content": {**PARSED["content"], "body": None}},
        {**PARSED, "content": {**PARSED["content"], "body": "   \n"}},
        {"metadata": PARSED["metadata"]},
        {"metadata": PARSED["metadata"], "content": "not an object"},
    ],
)
def test_parse_200_without_a_body_is_an_unavailable_article_422(payload):
    with pytest.raises(ArticleUnavailable) as exc:
        _download(lambda request: httpx.Response(200, json=payload))
    assert exc.value.status == 422


@pytest.mark.parametrize("response,status", [(_error(402), 402), (_error(403), 403), (_error(500), 500), (httpx.Response(302), 502)])
def test_parse_service_failures_stay_upstream_errors(response, status):
    with pytest.raises(UpstreamError) as exc:
        _download(lambda request: response)
    assert exc.value.status == status


def test_parse_non_object_json_is_an_upstream_error():
    with pytest.raises(UpstreamError):
        _download(lambda request: httpx.Response(200, json=["<p>body</p>"]))


def test_forged_non_numeric_bookmark_id_is_rejected_before_any_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=PARSED)

    with pytest.raises(ArticleUnavailable) as exc:
        _download(handler, _encode_article_id("../me", TIME, "T", None))
    assert exc.value.status == 404
    assert calls == []


def test_forged_non_ascii_digit_bookmark_id_is_rejected_before_any_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=PARSED)

    with pytest.raises(ArticleUnavailable) as exc:
        _download(handler, _encode_article_id("4²", TIME, "T", None))
    assert exc.value.status == 404
    assert calls == []


def test_list_then_download_agree_on_content_date():
    articles, _ = _list(lambda request: _page([BOOKMARK], 1))
    article, _ = _download(lambda request: httpx.Response(200, json=PARSED), articles[0].id)
    assert article.content_date == articles[0].content_date == datetime(2025, 1, 2, 1, 4, 5)
