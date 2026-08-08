import asyncio

import pytest

from later_ink.connectors.base import (
    VIEW_MAX_PAGES,
    VIEW_PAGE_TARGET,
    VIEW_SCAN_LIMIT,
    Article,
    Connector,
    Folder,
)
from later_ink.connectors.readwise import (
    BOOKS,
    LONG_READ_MIN_MINUTES,
    LONG_READS,
    SHORT_READ_MAX_MINUTES,
    SHORT_READS,
    VIEW_SOURCE_LOCATIONS,
    ReadwiseConnector,
)

MINUTE = 250  # words, at the base module's WORDS_PER_MINUTE


class PagedConnector(Connector):
    """Serves fixed pages per folder and records what was fetched, so a scan's
    cost and resume position can be asserted on."""

    name = "paged"
    description = "Paged"

    def __init__(self, pages_by_folder):
        self._pages = pages_by_folder
        self.fetches: list[tuple[str, str | None]] = []

    async def list_folders(self):
        return [Folder(fid, fid.title()) for fid in self._pages]

    async def list_articles(self, folder_id, cursor=None):
        self.fetches.append((folder_id, cursor))
        pages = self._pages[folder_id]
        index = int(cursor) if cursor else 0
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return pages[index], next_cursor

    async def get_article_html(self, article_id):
        return Article(id=article_id, title="x"), "<p>x</p>"


def _article(id_, words=None, category="article"):
    return Article(id=id_, title=f"Article {id_}", word_count=words, category=category)


# ----------------------------------------------------------- scan mechanics


def test_scan_walks_every_source_folder_and_ends_without_a_cursor():
    c = PagedConnector({"a": [[_article("1"), _article("2")]], "b": [[_article("3")]]})
    found, cursor = asyncio.run(c.scan_articles(lambda a: a.id != "2", ["a", "b"]))
    assert [a.id for a in found] == ["1", "3"]
    assert cursor is None


def test_scan_keeps_paging_past_pages_that_match_nothing():
    # The point of the scan: a view that is sparse in the first pages must not
    # hand the reader an empty screen while matches sit one page further on.
    c = PagedConnector(
        {"a": [[_article("1")], [_article("2")], [_article("wanted")]]}
    )
    found, cursor = asyncio.run(c.scan_articles(lambda a: a.id == "wanted", ["a"]))
    assert [a.id for a in found] == ["wanted"]
    assert cursor is None
    assert len(c.fetches) == 3


def test_scan_resumes_from_its_cursor_without_repeating_or_skipping():
    pages = [[_article(str(i)) for i in range(n * 10, n * 10 + 10)] for n in range(6)]
    c = PagedConnector({"a": pages})

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        found, cursor = asyncio.run(c.scan_articles(lambda a: True, ["a"], cursor))
        seen.extend(a.id for a in found)
        if cursor is None:
            break
    assert cursor is None
    assert seen == [a.id for page in pages for a in page]  # every item, once, in order


def test_scan_stops_at_the_page_target_and_returns_a_cursor():
    pages = [[_article(str(i)) for i in range(n * 20, n * 20 + 20)] for n in range(4)]
    c = PagedConnector({"a": pages})
    found, cursor = asyncio.run(c.scan_articles(lambda a: True, ["a"]))
    assert len(found) >= VIEW_PAGE_TARGET
    assert cursor is not None  # more to come, reachable through the next link


def test_scan_gives_up_after_the_page_budget_on_a_view_with_no_matches():
    c = PagedConnector({"a": [[_article(str(i))] for i in range(100)]})
    found, cursor = asyncio.run(c.scan_articles(lambda a: False, ["a"]))
    assert found == []
    assert len(c.fetches) == VIEW_MAX_PAGES  # bounded, and resumable from here
    assert cursor is not None


def test_scan_stops_examining_items_at_the_scan_limit():
    big = [_article(str(i)) for i in range(VIEW_SCAN_LIMIT)]
    c = PagedConnector({"a": [big, [_article("later-page")]]})
    found, cursor = asyncio.run(c.scan_articles(lambda a: a.id == "later-page", ["a"]))
    assert found == []
    assert len(c.fetches) == 1
    assert cursor is not None


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",  # unparseable
        "-3|x",  # before the start
        "999|stale",  # past the end: forged, or issued before the sources changed
    ],
)
def test_scan_survives_a_bad_cursor_by_starting_over(cursor):
    # Every unusable cursor restarts. An out-of-range one must not fall through
    # to an empty result, which a reader can't tell from "this view is empty".
    c = PagedConnector({"a": [[_article("1")]]})
    found, next_cursor = asyncio.run(c.scan_articles(lambda a: True, ["a"], cursor))
    assert [a.id for a in found] == ["1"]
    assert next_cursor is None


def test_scan_cursor_round_trips_an_upstream_cursor_containing_the_separator():
    class OddCursorConnector(PagedConnector):
        async def list_articles(self, folder_id, cursor=None):
            self.fetches.append((folder_id, cursor))
            return [_article("1")], None

    c = OddCursorConnector({"a": [[]]})
    found, _ = asyncio.run(c.scan_articles(lambda a: True, ["a"], "0|page|two"))
    assert c.fetches == [("a", "page|two")]  # the whole tail, not just "page"
    assert [a.id for a in found] == ["1"]


def test_base_connector_offers_no_views():
    c = PagedConnector({"a": [[]]})
    assert asyncio.run(c.list_views()) == []
    with pytest.raises(KeyError):
        asyncio.run(c.list_view_articles("short-reads"))


# ------------------------------------------------------------ readwise views


def _readwise(pages_by_location):
    """A ReadwiseConnector whose _get replays canned /list/ responses."""
    conn = ReadwiseConnector("tok")
    calls: list[dict] = []

    async def fake_get(path, params):
        calls.append(dict(params))
        key = params.get("location") or params.get("category")
        pages = pages_by_location.get(key, [[]])
        index = int(params.get("pageCursor", "0"))
        results = pages[index] if index < len(pages) else []
        return {
            "results": results,
            "nextPageCursor": str(index + 1) if index + 1 < len(pages) else None,
        }

    conn._get = fake_get
    return conn, calls


def _doc(id_, words, category="article"):
    return {"id": id_, "title": f"Doc {id_}", "word_count": words, "category": category}


def test_views_are_offered_and_do_not_collide_with_locations():
    conn = ReadwiseConnector("tok")
    views = asyncio.run(conn.list_views())
    assert [v.id for v in views] == ["short-reads", "long-reads", "books"]
    folder_ids = {f.id for f in asyncio.run(conn.list_folders())}
    assert folder_ids.isdisjoint({v.id for v in views})  # shared URL space
    asyncio.run(conn.close())


def test_books_view_hidden_when_epubs_are_not_served():
    conn = ReadwiseConnector("tok", categories=("article", "pdf"))
    assert BOOKS not in asyncio.run(conn.list_views())
    asyncio.run(conn.close())


def test_short_reads_takes_items_under_the_threshold_from_the_queue_locations():
    docs = [
        _doc("brief", 1 * MINUTE),
        _doc("borderline", SHORT_READ_MAX_MINUTES * MINUTE),  # not under it
        _doc("middling", 15 * MINUTE),
        _doc("epic", 40 * MINUTE),
    ]
    conn, calls = _readwise({loc: [docs] for loc in VIEW_SOURCE_LOCATIONS})
    found, _ = asyncio.run(conn.list_view_articles(SHORT_READS.id))
    assert {a.id for a in found} == {"brief"}
    assert [c["location"] for c in calls] == list(VIEW_SOURCE_LOCATIONS)
    assert "archive" not in [c["location"] for c in calls]
    asyncio.run(conn.close())


def test_long_reads_takes_items_over_the_threshold_and_leaves_books_out():
    docs = [
        _doc("epic", 40 * MINUTE),
        _doc("borderline", LONG_READ_MIN_MINUTES * MINUTE),  # not over it
        _doc("middling", 15 * MINUTE),
        _doc("novel", 300 * MINUTE, category="epub"),  # belongs in Books
    ]
    conn, _ = _readwise({loc: [docs] for loc in VIEW_SOURCE_LOCATIONS})
    found, _ = asyncio.run(conn.list_view_articles(LONG_READS.id))
    assert {a.id for a in found} == {"epic"}
    asyncio.run(conn.close())


def test_reading_time_views_skip_items_with_no_word_count():
    docs = [_doc("unknown", None), _doc("zero", 0)]
    conn, _ = _readwise({loc: [docs] for loc in VIEW_SOURCE_LOCATIONS})
    short, _ = asyncio.run(conn.list_view_articles(SHORT_READS.id))
    long_, _ = asyncio.run(conn.list_view_articles(LONG_READS.id))
    assert short == [] and long_ == []
    asyncio.run(conn.close())


def test_books_view_uses_the_native_category_filter_across_locations():
    conn, calls = _readwise(
        {"epub": [[_doc("book-1", 90 * MINUTE, category="epub")], [_doc("book-2", 5, "epub")]]}
    )
    found, cursor = asyncio.run(conn.list_view_articles(BOOKS.id))
    assert [a.id for a in found] == ["book-1"]
    assert cursor == "1"  # real upstream pagination, not a scan cursor
    assert calls == [{"category": "epub"}]  # no location: books live anywhere
    page2, cursor2 = asyncio.run(conn.list_view_articles(BOOKS.id, cursor))
    assert [a.id for a in page2] == ["book-2"] and cursor2 is None
    asyncio.run(conn.close())


def test_unknown_view_id_raises():
    conn = ReadwiseConnector("tok")
    with pytest.raises(KeyError):
        asyncio.run(conn.list_view_articles("nonsense"))
    asyncio.run(conn.close())
