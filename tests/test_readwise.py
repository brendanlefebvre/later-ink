import asyncio
from datetime import datetime

import httpx

from later_ink.connectors.readwise import ReadwiseConnector, _article_from_doc

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
