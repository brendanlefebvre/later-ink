import asyncio

from later_ink.connectors.readwise import ReadwiseConnector

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
