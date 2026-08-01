import asyncio

from read_later_opds.connectors.readwise import ReadwiseConnector

MIXED = {
    "results": [
        {"id": "1", "title": "An article", "category": "article"},
        {"id": "2", "title": "An email", "category": "email"},
        {"id": "3", "title": "A pdf", "category": "pdf"},
        {"id": "4", "title": "A book", "category": "epub"},
        {"id": "5", "title": "A tweet", "category": "tweet"},
        {"id": "6", "title": "A video", "category": "youtube"},
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


def test_default_categories_cover_articles_emails_pdfs_epubs():
    articles, cursor = _list(ReadwiseConnector("tok"))
    # article + email + pdf + epub, not tweet/video
    assert {a.id for a in articles} == {"1", "2", "3", "4"}
    assert cursor == "next123"  # pagination cursor passed through


def test_categories_configurable():
    articles, _ = _list(ReadwiseConnector("tok", categories=("article", "pdf")))
    assert {a.id for a in articles} == {"1", "3"}
