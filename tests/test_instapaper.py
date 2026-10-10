import asyncio
import base64
import json
from datetime import datetime

import httpx
import pytest

from later_ink.connectors.base import ArticleUnavailable, UpstreamError
from later_ink.connectors.instapaper import (
    InstapaperConnector,
    _decode_article_id,
    _encode_article_id,
    _OAuth1Auth,
    _parse_error_envelope,
    _raise_for_get_text_error,
    _raise_for_json_error,
)


def test_oauth1_auth_signs_with_pinned_nonce_and_timestamp():
    auth = _OAuth1Auth("ck", "cs", "ot", "ots")
    # Pin nonce/timestamp so the signature is deterministic and we assert the
    # real base-string construction, not merely "a header exists".
    auth._client.nonce = "pinned-nonce"
    auth._client.timestamp = "1700000000"

    request = httpx.Request(
        "POST",
        "https://www.instapaper.com/api/1/bookmarks/list",
        data={"folder_id": "unread", "limit": "500"},
    )
    flow = auth.auth_flow(request)
    signed = next(flow)

    header = signed.headers["Authorization"]
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in header
    assert 'oauth_token="ot"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_nonce="pinned-nonce"' in header
    assert 'oauth_timestamp="1700000000"' in header
    # HMAC-SHA1 with key "cs&ots" over the RFC 5849 base string for this
    # request, computed independently of oauthlib. Includes the form-body
    # params, so a signer that dropped the body would produce a different value.
    assert 'oauth_signature="J1PvH2e2f3l0p5xsvGoXO0qtidI%3D"' in header


def test_article_id_round_trips():
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "https://example.com/a")


def test_article_id_round_trips_empty_url():
    token = _encode_article_id("42", 1735779845, "Some Title", None)
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "")


def test_article_id_token_is_url_path_safe():
    token = _encode_article_id("42", 1735779845, "Café — déjà vu", "https://example.com/a?x=1")
    # base64url alphabet only (plus no padding); safe to drop into a URL path.
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


def test_parse_error_envelope_detects_list_and_bare_forms():
    assert _parse_error_envelope('[{"type":"error","error_code":1241}]')["error_code"] == 1241
    assert _parse_error_envelope('{"type":"error","error_code":1550}')["error_code"] == 1550


def test_parse_error_envelope_returns_none_for_html_and_non_error_json():
    assert _parse_error_envelope("<article><p>Body</p></article>") is None
    assert _parse_error_envelope('[{"type":"bookmark","bookmark_id":1}]') is None


def test_raise_for_json_error_maps_rate_limit_and_suspension():
    with pytest.raises(UpstreamError) as e1:
        _raise_for_json_error([{"type": "error", "error_code": 1040}])
    assert e1.value.status == 429
    with pytest.raises(UpstreamError) as e2:
        _raise_for_json_error([{"type": "error", "error_code": 1042}])
    assert e2.value.status == 403


def test_raise_for_json_error_passes_clean_list():
    assert _raise_for_json_error([{"type": "bookmark", "bookmark_id": 1}]) is None


def test_raise_for_json_error_unknown_code_is_502():
    with pytest.raises(UpstreamError) as e:
        _raise_for_json_error([{"type": "error", "error_code": 1500}])
    assert e.value.status == 502


def test_raise_for_get_text_error_maps_codes():
    with pytest.raises(ArticleUnavailable) as e_missing:
        _raise_for_get_text_error({"type": "error", "error_code": 1241})
    assert e_missing.value.status == 404

    for code in (1041, 1220, 1221, 1550):
        with pytest.raises(ArticleUnavailable) as e:
            _raise_for_get_text_error({"type": "error", "error_code": code})
        assert e.value.status == 422

    with pytest.raises(UpstreamError) as e_rate:
        _raise_for_get_text_error({"type": "error", "error_code": 1040})
    assert e_rate.value.status == 429


def test_raise_for_get_text_error_unknown_code_is_article_unavailable_422():
    with pytest.raises(ArticleUnavailable) as e:
        _raise_for_get_text_error({"type": "error", "error_code": 9999})
    assert e.value.status == 422


_BOOKMARK = {
    "type": "bookmark",
    "bookmark_id": 42,
    "title": "Some Title",
    "url": "https://example.com/a",
    "description": "An excerpt",
    "time": 1735779845,
}


def _conn(handler):
    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
    )
    return InstapaperConnector("ck", "cs", "ot", "ots", client=client), client


def test_list_folders_has_builtins_then_custom():
    def handler(request):
        assert request.url.path.endswith("/folders/list")
        return httpx.Response(200, json=[{"folder_id": 100, "title": "Recipes"}])

    conn, client = _conn(handler)

    async def go():
        folders = await conn.list_folders()
        await conn.close()
        return folders

    folders = asyncio.run(go())
    assert [f.id for f in folders] == ["unread", "starred", "archive", "100"]
    assert folders[-1].title == "Recipes"


def test_list_articles_maps_bookmark_and_has_no_cursor():
    def handler(request):
        assert request.url.path.endswith("/bookmarks/list")
        return httpx.Response(200, json=[{"type": "user", "user_id": 1}, _BOOKMARK])

    conn, client = _conn(handler)

    async def go():
        result = await conn.list_articles("unread")
        await conn.close()
        return result

    articles, cursor = asyncio.run(go())
    assert cursor is None
    assert len(articles) == 1
    a = articles[0]
    assert a.title == "Some Title"
    assert a.url == "https://example.com/a"
    assert a.summary == "An excerpt"
    assert a.content_date == datetime(2025, 1, 2, 1, 4, 5)


def test_list_articles_rejects_non_list_shape():
    def handler(request):
        return httpx.Response(200, json={"type": "error", "error_code": 1500})

    conn, client = _conn(handler)

    async def go():
        try:
            await conn.list_articles("unread")
        finally:
            await conn.close()

    with pytest.raises(UpstreamError):
        asyncio.run(go())


def test_get_article_html_returns_html_and_reconstructs_metadata():
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")

    def handler(request):
        assert request.url.path.endswith("/bookmarks/get_text")
        return httpx.Response(200, text="<article><p>Body</p></article>")

    conn, client = _conn(handler)

    async def go():
        result = await conn.get_article_html(token)
        await conn.close()
        return result

    article, html = asyncio.run(go())
    assert "<p>Body</p>" in html
    assert article.title == "Some Title"
    assert article.url == "https://example.com/a"
    assert article.content_date == datetime(2025, 1, 2, 1, 4, 5)


def test_get_article_html_maps_error_envelope_under_http_200():
    token = _encode_article_id("42", 1735779845, "t", "u")

    def handler(request):
        # An error envelope arriving under HTTP 200 must still be treated as an error.
        return httpx.Response(200, json=[{"type": "error", "error_code": 1241}])

    conn, client = _conn(handler)

    async def go():
        try:
            await conn.get_article_html(token)
        finally:
            await conn.close()

    with pytest.raises(ArticleUnavailable) as e:
        asyncio.run(go())
    assert e.value.status == 404


def test_article_id_round_trips_missing_time():
    token = _encode_article_id("42", None, "t", "u")
    assert _decode_article_id(token) == ("42", None, "t", "u")


def _list_then_download(bookmark):
    """List a folder holding `bookmark` and a good one, then download each by its
    listed id — the two paths the determinism contract requires to agree."""

    def handler(request):
        if request.url.path.endswith("/bookmarks/list"):
            return httpx.Response(200, json=[bookmark, _BOOKMARK])
        return httpx.Response(200, text="<article><p>Body</p></article>")

    conn, _client = _conn(handler)

    async def go():
        try:
            listed, _ = await conn.list_articles("unread")
            downloaded = [(await conn.get_article_html(a.id))[0] for a in listed]
            return listed, downloaded
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.mark.parametrize(
    "bad_time", [None, "", "not-an-int"], ids=["missing", "blank", "non-numeric"]
)
def test_unusable_time_gives_no_content_date_on_both_paths(bad_time):
    bookmark = {"type": "bookmark", "bookmark_id": 7, "title": "t", "url": "u"}
    if bad_time is not None:
        bookmark["time"] = bad_time

    listed, downloaded = _list_then_download(bookmark)

    # One bad bookmark must not take the folder down: the good one still lists.
    assert [a.title for a in listed] == ["t", "Some Title"]
    # Unknown, not the epoch: list and download must agree on None.
    assert listed[0].content_date is None
    assert downloaded[0].content_date is None
    # The good bookmark is unaffected on either path.
    assert listed[1].content_date == downloaded[1].content_date == datetime(2025, 1, 2, 1, 4, 5)


def _download_with(response: httpx.Response):
    """Download one article against a get_text that answers with `response`."""
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")
    conn, _client = _conn(lambda request: response)

    async def go():
        try:
            return await conn.get_article_html(token)
        finally:
            await conn.close()

    return asyncio.run(go())


def test_get_text_bare_400_is_an_unavailable_article():
    # Live, get_text's 400s carry an error envelope (1241, 1550). A 400 that
    # arrives without one is still about the article, not the service, so the
    # reader gets a readable reason rather than a generic upstream error.
    with pytest.raises(ArticleUnavailable) as e:
        _download_with(httpx.Response(400, text="Bad Request"))
    assert e.value.status == 422


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_get_text_bare_service_failures_stay_upstream_errors(status):
    # A rejected token or a failing service must not be reported as a bad article.
    with pytest.raises(UpstreamError) as e:
        _download_with(httpx.Response(status, text=""))
    assert e.value.status == status


def test_get_text_redirect_is_not_accepted_as_the_article():
    # httpx doesn't follow redirects, so without a 2xx check a redirect's body
    # (a sign-in or interstitial page) would be turned into the EPUB.
    with pytest.raises(UpstreamError):
        _download_with(
            httpx.Response(
                302,
                headers={"Location": "http://portal.example/login"},
                text="<html><body>Please sign in to the Wi-Fi</body></html>",
            )
        )


def test_list_redirect_is_not_accepted_as_a_listing():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://elsewhere.example/"}, json=[_BOOKMARK])

    conn, _client = _conn(handler)

    async def go():
        try:
            await conn.list_articles("unread")
        finally:
            await conn.close()

    with pytest.raises(UpstreamError):
        asyncio.run(go())


def test_oauth1_auth_signs_a_request_with_no_body():
    # folders/list takes no parameters, so its POST has an empty body. Passing a
    # form Content-Type for an empty body made oauthlib raise ValueError, which
    # escaped as a 500 on the whole Instapaper catalog.
    auth = _OAuth1Auth("ck", "cs", "ot", "ots")
    request = httpx.Request("POST", "https://www.instapaper.com/api/1/folders/list", data={})
    signed = next(auth.auth_flow(request))
    assert signed.headers["Authorization"].startswith("OAuth ")


def test_signed_client_drives_list_folders_and_list_articles():
    # Every other test injects an unsigned client, so the signer never sees the
    # connector's real requests. This one keeps the signer on the injected client.
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("Authorization", "")))
        if request.url.path.endswith("/folders/list"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[_BOOKMARK])

    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
        auth=_OAuth1Auth("ck", "cs", "ot", "ots"),
    )
    conn = InstapaperConnector("ck", "cs", "ot", "ots", client=client)

    async def go():
        try:
            await conn.list_folders()
            await conn.list_articles("unread")
        finally:
            await conn.close()

    asyncio.run(go())
    assert [p.rsplit("/", 1)[-1] for p, _ in seen] == ["list", "list"]
    assert all(h.startswith("OAuth ") for _, h in seen)
