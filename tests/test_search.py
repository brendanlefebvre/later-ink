import asyncio

from later_ink.connectors.base import (
    SEARCH_SCAN_LIMIT,
    Article,
    Connector,
    Folder,
)


class FakeConnector(Connector):
    name = "fake"
    description = "Fake"

    def __init__(self, folders, articles_by_folder):
        self._folders = folders
        self._articles = articles_by_folder

    async def list_folders(self):
        return self._folders

    async def list_articles(self, folder_id, cursor=None):
        return self._articles.get(folder_id, []), None

    async def get_article_html(self, article_id):
        return Article(id=article_id, title="x"), "<p>x</p>"


def test_search_matches_title_and_summary_case_insensitively():
    c = FakeConnector(
        [Folder("a", "A"), Folder("b", "B")],
        {
            "a": [Article(id="1", title="Rust ownership"), Article(id="2", title="Python", author="Guido")],
            "b": [Article(id="3", title="Notes", summary="about the RUST borrow checker")],
        },
    )
    res, cursor = asyncio.run(c.search("rust"))
    assert {a.id for a in res} == {"1", "3"}  # title + summary hit; unrelated item excluded
    assert cursor is None


def test_search_matches_author():
    c = FakeConnector([Folder("a", "A")], {"a": [Article(id="1", title="Post", author="Ada Lovelace")]})
    res, _ = asyncio.run(c.search("lovelace"))
    assert [a.id for a in res] == ["1"]


def test_search_blank_query_returns_nothing():
    c = FakeConnector([Folder("a", "A")], {"a": [Article(id="1", title="anything")]})
    res, cursor = asyncio.run(c.search("   "))
    assert res == [] and cursor is None


def test_search_dedupes_articles_seen_in_multiple_folders():
    dup = Article(id="1", title="dup about rust")
    c = FakeConnector([Folder("a", "A"), Folder("b", "B")], {"a": [dup], "b": [dup]})
    res, _ = asyncio.run(c.search("rust"))
    assert len(res) == 1


def test_search_stops_at_scan_limit_within_a_single_page():
    # A single oversized page must not be scanned past the cap: only the first
    # SEARCH_SCAN_LIMIT items are considered, even though every item matches.
    oversized = [Article(id=str(i), title=f"rust {i}") for i in range(SEARCH_SCAN_LIMIT + 50)]
    c = FakeConnector([Folder("a", "A")], {"a": oversized})
    res, cursor = asyncio.run(c.search("rust"))
    assert len(res) == SEARCH_SCAN_LIMIT  # items beyond the cap were never processed
    assert cursor is None
