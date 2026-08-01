import asyncio

from read_later_opds.connectors.readwise import ReadwiseConnector

MIXED = {
    "results": [
        {"id": "1", "title": "An article", "category": "article"},
        {"id": "2", "title": "A book", "category": "book"},
        {"id": "3", "title": "A tweet", "category": "tweet"},
        {"id": "4", "title": "A video", "category": "youtube"},
        {"id": "5", "title": "A pdf", "category": "pdf"},
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


def test_default_categories_are_articles_and_books():
    articles, cursor = _list(ReadwiseConnector("tok"))
    assert {a.id for a in articles} == {"1", "2"}  # article + book, not tweet/video/pdf
    assert cursor == "next123"  # pagination cursor passed through


def test_categories_configurable():
    articles, _ = _list(ReadwiseConnector("tok", categories=("article", "pdf")))
    assert {a.id for a in articles} == {"1", "5"}
