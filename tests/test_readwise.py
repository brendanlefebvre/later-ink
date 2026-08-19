import asyncio
from datetime import datetime

import httpx
import pytest

from later_ink.connectors.base import Article, UpstreamError, minutes_to_words
from later_ink.connectors.readwise import (
    BOOK_CATEGORY,
    LONG_READ_MIN_MINUTES,
    SHORT_READ_MAX_MINUTES,
    ReadwiseConnector,
    _article_from_doc,
    _reading_time_view,
)

MIXED = {
    "results": [
        {"id": "1", "title": "An article", "category": "article"},
        {"id": "2", "title": "An email", "category": "email"},
        {"id": "3", "title": "A pdf", "category": "pdf"},
        {"id": "4", "title": "A book", "category": "epub"},
        {"id": "5", "title": "A thread", "category": "tweet"},
        {"id": "6", "title": "A video", "category": "video"},
        {"id": "7", "title": "A podcast", "category": "podcast"},
    ],
    "nextPageCursor": "next123",
}


def _list(conn: ReadwiseConnector):
    async def run():
        async def fake_get(path, params):
            assert "category" not in params  # we fetch the whole location now
            return MIXED
        conn._get = fake_get
        try:
            return await conn.list_articles("later")
        finally:
            await conn.close()

    return asyncio.run(run())


def test_default_categories_cover_all_content_types():
    articles, cursor = _list(ReadwiseConnector("tok"))
    # article, email, pdf, epub, tweet, video, podcast — all included
    assert {a.id for a in articles} == {"1", "2", "3", "4", "5", "6", "7"}
    assert cursor == "next123"  # pagination cursor passed through


def test_categories_configurable():
    articles, _ = _list(ReadwiseConnector("tok", categories=("article", "pdf")))
    assert {a.id for a in articles} == {"1", "3"}


def test_unloaded_podcast_stub_gives_load_transcript_message():
    import pytest

    from later_ink.connectors.base import ArticleUnavailable

    conn = ReadwiseConnector("tok")
    stub = (
        "<div class='rw-podcast-description'>"
        "<a data-rw-button='load-podcast-transcript'>Load Transcript</a></div>"
    )

    async def run():
        async def fake_get(path, params):
            return {"results": [{"id": "9", "title": "Pod", "html_content": stub}]}

        conn._get = fake_get
        try:
            return await conn.get_article_html("9")
        finally:
            await conn.close()

    with pytest.raises(ArticleUnavailable) as exc:
        asyncio.run(run())
    assert exc.value.status == 422
    assert "transcript" in str(exc.value).lower()


def test_article_content_date_prefers_saved_at():
    art = _article_from_doc(
        {"id": 1, "title": "T", "saved_at": "2025-03-04T05:06:07Z", "created_at": "2024-01-01T00:00:00Z"}
    )
    assert art.content_date == datetime(2025, 3, 4, 5, 6, 7)


def test_article_content_date_falls_back_to_created_at():
    art = _article_from_doc({"id": 1, "title": "T", "created_at": "2024-01-01T00:00:00Z"})
    assert art.content_date == datetime(2024, 1, 1, 0, 0, 0)


def test_article_content_date_is_none_without_dates():
    assert _article_from_doc({"id": 1, "title": "T"}).content_date is None


def test_article_content_date_normalized_to_utc():
    # ebooklib formats this value with strftime("%Y-%m-%dT%H:%M:%SZ") — it
    # appends a literal Z without converting, so a non-UTC offset would be
    # written as UTC while carrying local wall-clock time.
    art = _article_from_doc({"id": 1, "title": "T", "saved_at": "2025-03-04T05:06:07+02:00"})
    assert art.content_date == datetime(2025, 3, 4, 3, 6, 7)
    assert art.content_date.tzinfo is None


def test_an_injected_client_is_used_instead_of_a_real_one():
    # The seam the contract suite needs: a connector must be constructable
    # against a mock transport without reaching into its privates.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "nextPageCursor": None})

    async def run():
        client = httpx.AsyncClient(
            base_url="https://readwise.test", transport=httpx.MockTransport(handler)
        )
        conn = ReadwiseConnector("tok", client=client)
        try:
            await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())
    assert seen and "readwise.test" in seen[0]


def test_a_non_json_response_is_reported_as_an_upstream_error():
    # An HTTP 200 carrying a proxy error page rather than JSON. Left unguarded
    # this raises JSONDecodeError, which is not UpstreamError, so it escapes as
    # a 500 instead of a readable message on the e-reader.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            with pytest.raises(UpstreamError):
                await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())


def _article(words: int | None, category: str = "article") -> Article:
    return Article(id="1", title="T", word_count=words, category=category)


def test_short_reads_takes_articles_under_the_threshold():
    assert _reading_time_view(_article(minutes_to_words(SHORT_READ_MAX_MINUTES) - 1), short=True)
    assert not _reading_time_view(_article(minutes_to_words(SHORT_READ_MAX_MINUTES) + 1), short=True)


def test_long_reads_takes_articles_over_the_threshold():
    assert _reading_time_view(_article(minutes_to_words(LONG_READ_MIN_MINUTES) + 1), short=False)
    assert not _reading_time_view(_article(minutes_to_words(LONG_READ_MIN_MINUTES) - 1), short=False)


def test_an_unknown_length_is_not_a_short_read():
    # A missing word count means unknown length, not zero — it must not fall
    # into Short reads by default.
    assert not _reading_time_view(_article(None), short=True)
    assert not _reading_time_view(_article(0), short=True)


def test_books_are_excluded_from_long_reads():
    # Books have their own list, and would otherwise be most of Long reads.
    long_enough = minutes_to_words(LONG_READ_MIN_MINUTES) + 1
    assert not _reading_time_view(_article(long_enough, category=BOOK_CATEGORY), short=False)


def test_books_are_listed_by_category_and_paginated():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [{"id": "9", "title": "A book", "category": "epub"}],
                "nextPageCursor": "page2",
            },
        )

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            return await conn._list_books(cursor="page1")
        finally:
            await conn.close()

    articles, cursor = asyncio.run(run())
    assert [a.id for a in articles] == ["9"]
    assert cursor == "page2"
    assert seen[0]["category"] == "epub"
    assert seen[0]["pageCursor"] == "page1"


def test_categories_outside_the_configured_set_are_dropped():
    articles, _ = _list(ReadwiseConnector("tok", categories=("article",)))
    assert [a.category for a in articles] == ["article"]


def test_a_429_is_retried_once_and_honours_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(asyncio, "sleep", lambda d: _noop(slept, d))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"results": [], "nextPageCursor": None})

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            return await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())
    assert len(attempts) == 2       # retried once
    assert slept == [3.0]           # honoured Retry-After


def test_a_persistent_429_becomes_a_readable_error(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda d: _noop([], d))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            with pytest.raises(UpstreamError) as caught:
                await conn.list_articles("later")
            assert caught.value.status == 429
        finally:
            await conn.close()

    asyncio.run(run())


async def _noop(record, delay):
    record.append(delay)
