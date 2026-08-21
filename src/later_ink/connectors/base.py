import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx


class UpstreamError(Exception):
    """The upstream read-it-later service failed; surface a readable message."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ArticleUnavailable(Exception):
    """The item is in the catalog but can't be turned into an EPUB — missing
    upstream, or with no extractable article text. Surfaced to the client as a
    readable message rather than a 500 so e-reader users know why a download
    failed."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Folder:
    id: str
    title: str
    description: str = ""


@dataclass
class Article:
    id: str
    title: str
    author: str | None = None
    summary: str | None = None
    url: str | None = None
    updated: datetime = field(default_factory=datetime.now)
    word_count: int | None = None
    language: str | None = None
    category: str | None = None
    image_url: str | None = None
    # When this content entered the user's library. Feeds the EPUB's
    # dcterms:modified, so it must be stable: a value that moves when an
    # article is archived or starred would rewrite the file and reset the
    # reader's progress. Distinct from `updated` above, which defaults to
    # now() and is not usable for that.
    content_date: datetime | None = None


# Bound the client-side search scan for connectors without a native full-text
# endpoint: search looks at folders until it has scanned this many items, then
# stops, so one query on a huge library can't turn into an unbounded crawl.
SEARCH_SCAN_LIMIT = 400

# Reading-time estimate for word-count filters. 250 wpm is the usual adult
# prose figure and the one read-it-later apps quote, so "10 minutes" here means
# roughly what it means in Readwise Reader.
WORDS_PER_MINUTE = 250

# Bounds for a view scan (see Connector.scan_articles). A view is assembled by
# paging through folders and filtering client-side, so each request stops at
# the first of: enough matches for a full page, too many items examined, or too
# many upstream calls. Whatever is left is reachable through the next cursor,
# so these cap one request's cost, not the view's reach.
VIEW_PAGE_TARGET = 25
VIEW_SCAN_LIMIT = 400
VIEW_MAX_PAGES = 12


def minutes_to_words(minutes: int) -> int:
    return minutes * WORDS_PER_MINUTE


def parse_dt(value: str | None) -> datetime | None:
    """Parse an upstream ISO timestamp as naive UTC, or None.

    Shared by every connector that feeds Article.content_date: normalized to
    UTC with the tzinfo dropped because ebooklib writes dcterms:modified with
    strftime("%Y-%m-%dT%H:%M:%SZ") — it appends the Z without converting, so
    an aware non-UTC value would be labelled UTC while carrying local
    wall-clock time.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def retry_after_seconds(resp: httpx.Response, *, default: float = 2.0, cap: float = 15.0) -> float:
    """How long to wait after a 429, from Retry-After, bounded.

    Bounded because an upstream is free to say "an hour" and an e-reader waiting
    on a download is not — past the cap, failing readably beats hanging.

    A malformed value falls back to the default rather than being trusted:
    float() happily parses "-1", "nan", and "inf" without raising, so a plain
    try/except ValueError lets them through. A negative delay skips backoff
    entirely, and NaN reaching asyncio.sleep raises ValueError there instead —
    not a slow download, a 500.
    """
    try:
        seconds = float(resp.headers.get("Retry-After", default))
    except ValueError:
        return default
    if not math.isfinite(seconds) or seconds < 0:
        return default
    return min(seconds, cap)


def raise_for_upstream(resp: httpx.Response, service: str) -> None:
    """Turn an error status into an UpstreamError, or return for a good one.

    service names the upstream in the message, because the person reading it on
    an e-reader needs to know which account to go and fix.
    """
    if resp.status_code == 429:
        raise UpstreamError(f"{service} is rate-limiting this account; try again in a minute", 429)
    if resp.status_code == 401:
        raise UpstreamError(f"{service} rejected the stored credentials", 401)
    if resp.status_code >= 400:
        raise UpstreamError(f"{service} returned an error ({resp.status_code})", resp.status_code)


def decode_json(resp: httpx.Response, service: str) -> Any:
    """Parse a response body, or raise UpstreamError.

    A 200 is not a promise of JSON: a proxy or captive portal answers with an
    HTML error page and the status of its own choosing. Unguarded, that raises
    JSONDecodeError — not an UpstreamError — and reaches the reader as a 500.

    Typed `Any`, not `dict`: resp.json() returns whatever the body decodes to,
    and a top-level JSON array decodes to a list. Both current upstreams
    return objects, so `dict` was true by accident — a future connector whose
    endpoint returns a top-level array would be lying about its own return
    type. The two connectors' own `_get(...) -> dict` stay as `dict`, which is
    accurate for those two upstreams specifically.
    """
    try:
        return resp.json()
    except ValueError as e:
        raise UpstreamError(f"{service} returned an unexpected response") from e


def _encode_scan_cursor(folder_index: int, page: str | None) -> str:
    """Position in a view scan: which source folder, and where inside it.

    The upstream page cursor is kept verbatim after the separator, so it may
    itself contain "|" — decoding splits once, from the left.
    """
    return f"{folder_index}|{page or ''}"


def _decode_scan_cursor(cursor: str | None) -> tuple[int, str | None]:
    """Inverse of _encode_scan_cursor; (0, None) — start over — if unusable.

    An unusable folder index discards the upstream page along with it: half a
    cursor is not better than none, since resuming a fresh folder from a page
    belonging to some other one is how you skip items silently.
    """
    if not cursor:
        return 0, None
    index, _, page = cursor.partition("|")
    try:
        folder_index = int(index)
    except ValueError:
        return 0, None  # malformed (or a cursor from an older release)
    if folder_index < 0:
        return 0, None
    return folder_index, page or None


class Connector(ABC):
    name: str
    description: str

    @abstractmethod
    async def list_folders(self) -> list[Folder]:
        ...

    @abstractmethod
    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Return (articles, next_cursor). next_cursor is None when no more pages."""
        ...

    @abstractmethod
    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        """Return (article_metadata, html_content)."""
        ...

    async def list_views(self) -> list[Folder]:
        """Extra catalog entries that cut across locations rather than being
        one — "Short reads", "Books", and the like.

        They appear alongside the real folders in the navigation feed and are
        browsed through the same URL, so their ids must not collide with folder
        ids. Empty by default: a connector only offers the views its upstream
        supplies the metadata for."""
        return []

    async def list_view_articles(
        self, view_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Return (articles, next_cursor) for one of `list_views()`.

        Only called for ids that came from `list_views()`, so the base class —
        which offers none — never reaches this."""
        raise KeyError(view_id)

    async def scan_articles(
        self,
        matches: Callable[[Article], bool],
        folder_ids: Sequence[str],
        cursor: str | None = None,
    ) -> tuple[list[Article], str | None]:
        """Page through `folder_ids` in order, keeping the articles that match.

        For views the upstream API can't express as a query (a word-count
        filter, say), so they have to be assembled here. Returns one page worth
        of matches plus a cursor to resume from, or None once every source
        folder is exhausted.

        Sparse views are the reason for the scan bounds rather than a plain
        page-at-a-time filter: if one upstream page yields no matches, this
        keeps fetching instead of handing the reader an empty screen, up to the
        VIEW_* caps.
        """
        folder_index, page = _decode_scan_cursor(cursor)
        if folder_index >= len(folder_ids):
            # Past the end: a cursor forged by hand, or a real one issued before
            # the source folders changed under it. Restart rather than return an
            # empty list, which would read as "this view is empty".
            folder_index, page = 0, None

        matched: list[Article] = []
        seen: set[str] = set()
        scanned = fetched = 0

        while folder_index < len(folder_ids):
            if (
                len(matched) >= VIEW_PAGE_TARGET
                or scanned >= VIEW_SCAN_LIMIT
                or fetched >= VIEW_MAX_PAGES
            ):
                # Stop on a page boundary so the cursor names the next unread
                # page exactly — no item is served twice or skipped.
                return matched, _encode_scan_cursor(folder_index, page)

            articles, next_page = await self.list_articles(folder_ids[folder_index], page)
            fetched += 1
            scanned += len(articles)
            for article in articles:
                if article.id not in seen and matches(article):
                    seen.add(article.id)
                    matched.append(article)

            if next_page:
                page = next_page
            else:
                folder_index += 1
                page = None

        return matched, None

    async def search(
        self, query: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Find articles whose title, author, or summary contain `query`.

        Default implementation: scan the connector's folders client-side and
        filter — enough for services (like Readwise) that expose no full-text
        search endpoint. Bounded by SEARCH_SCAN_LIMIT. Connectors with a native
        search endpoint should override. Returns a single page (no cursor)."""
        needle = query.strip().lower()
        if not needle:
            return [], None
        seen: set[str] = set()
        matches: list[Article] = []
        scanned = 0
        for folder in await self.list_folders():
            page: str | None = None
            while scanned < SEARCH_SCAN_LIMIT:
                articles, page = await self.list_articles(folder.id, page)
                for a in articles:
                    if scanned >= SEARCH_SCAN_LIMIT:
                        break  # stop mid-page so a huge page can't blow the cap
                    scanned += 1
                    if a.id in seen:
                        continue
                    haystack = " ".join(p for p in (a.title, a.author, a.summary) if p).lower()
                    if needle in haystack:
                        seen.add(a.id)
                        matches.append(a)
                if not page or scanned >= SEARCH_SCAN_LIMIT:
                    break
            if scanned >= SEARCH_SCAN_LIMIT:
                break
        return matches, None
