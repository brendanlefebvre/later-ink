from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


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


# Bound the client-side search scan for connectors without a native full-text
# endpoint: search looks at folders until it has scanned this many items, then
# stops, so one query on a huge library can't turn into an unbounded crawl.
SEARCH_SCAN_LIMIT = 400


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
                    scanned += 1
                    if a.id in seen:
                        continue
                    haystack = " ".join(p for p in (a.title, a.author, a.summary) if p).lower()
                    if needle in haystack:
                        seen.add(a.id)
                        matches.append(a)
                if not page:
                    break
            if scanned >= SEARCH_SCAN_LIMIT:
                break
        return matches, None
