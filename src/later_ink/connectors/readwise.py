import asyncio
from datetime import UTC, datetime

import httpx

from .base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
    minutes_to_words,
)

BASE_URL = "https://readwise.io/api/v3"


async def validate_token(token: str) -> bool:
    """True if the token is accepted by the Readwise API.

    `updatedAfter=now` yields an empty page — auth is checked without pulling
    the user's whole document list or denting their rate limit mid-onboarding.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{BASE_URL}/list/",
            params={"updatedAfter": now_iso},
            headers={"Authorization": f"Token {token}"},
        )
    return resp.status_code == 200


LOCATIONS = [
    Folder("later", "Later", "Articles saved for later"),
    Folder("new", "New", "Recently added articles"),
    Folder("shortlist", "Shortlist", "Shortlisted articles"),
    Folder("archive", "Archive", "Archived articles"),
    Folder("feed", "Feed", "Feed articles"),
]

# Readwise categories we serve, all delivered as EPUBs converted from the
# document's html_content — which Readwise populates for every one of these:
# PDFs and uploaded EPUBs as document text, videos as transcripts, tweets as
# unrolled threads. Podcasts work only once their transcript has been loaded in
# Reader (until then html_content is a "Load Transcript" stub — see
# _PODCAST_STUB_MARKER). Note: Reader's category for uploaded books is "epub"
# (not "book"), and for videos is "video" (not "youtube").
DEFAULT_CATEGORIES = ("article", "email", "pdf", "epub", "video", "tweet", "podcast")

# Present in html_content only while a podcast transcript is still lazy-loaded.
_PODCAST_STUB_MARKER = "load-podcast-transcript"

# Reader's own filtered-view examples use reading_time:<10 and >20, so the same
# article lands in the same list here as it does in the app. The gap between
# them is deliberate: a 15-minute read is neither a short nor a long one.
SHORT_READ_MAX_MINUTES = 10
LONG_READ_MIN_MINUTES = 20

# Which locations the reading-time views draw from. Archive is left out (those
# are finished) and so is Feed (subscription items you haven't chosen to save),
# which together keeps these lists to "things I meant to read" — and keeps the
# scan short enough to stay responsive on e-ink.
VIEW_SOURCE_LOCATIONS = ("later", "shortlist", "new")

# The category Reader files uploaded books under (not "book").
BOOK_CATEGORY = "epub"

SHORT_READS = Folder(
    "short-reads", "Short reads", f"Under {SHORT_READ_MAX_MINUTES} minutes, from your queue"
)
LONG_READS = Folder(
    "long-reads", "Long reads", f"Over {LONG_READ_MIN_MINUTES} minutes, from your queue"
)
BOOKS = Folder("books", "Books", "EPUBs you've uploaded to Reader")


def _reading_time_view(article: Article, *, short: bool) -> bool:
    """Whether an article belongs in the short- or long-reads list.

    Skips anything Readwise has no word count for — a missing count is unknown
    length, not zero — and skips books, which have their own list and would
    otherwise be the only thing in Long reads.
    """
    words = article.word_count
    if not words or words <= 0 or article.category == BOOK_CATEGORY:
        return False
    if short:
        return words < minutes_to_words(SHORT_READ_MAX_MINUTES)
    return words > minutes_to_words(LONG_READ_MIN_MINUTES)


def _article_from_doc(doc: dict) -> Article:
    return Article(
        id=str(doc["id"]),
        title=doc.get("title") or "Untitled",
        author=doc.get("author"),
        summary=doc.get("summary"),
        url=doc.get("source_url") or doc.get("url"),
        word_count=doc.get("word_count"),
        language=doc.get("language"),
        category=doc.get("category"),
        image_url=doc.get("image_url"),
    )


class ReadwiseConnector(Connector):
    name = "readwise"
    description = "Readwise Reader"

    def __init__(self, token: str, categories: tuple[str, ...] = DEFAULT_CATEGORIES):
        self._token = token
        self._categories = set(categories)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Token {token}"},
            timeout=30.0,
        )

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        """GET with one retry on 429 (honoring Retry-After) and readable errors."""
        for attempt in (0, 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as e:
                raise UpstreamError(f"Could not reach Readwise: {type(e).__name__}") from e
            if resp.status_code == 429 and attempt == 0:
                try:
                    delay = min(float(resp.headers.get("Retry-After", "2")), 15.0)
                except ValueError:
                    delay = 2.0
                await asyncio.sleep(delay)
                continue
            break
        if resp.status_code == 429:
            raise UpstreamError(
                "Readwise is rate-limiting this account; try again in a minute", 429
            )
        if resp.status_code == 401:
            raise UpstreamError("Readwise rejected the stored token", 401)
        if resp.status_code >= 400:
            raise UpstreamError(f"Readwise returned an error ({resp.status_code})", resp.status_code)
        return resp.json()

    async def list_folders(self) -> list[Folder]:
        return LOCATIONS

    async def list_views(self) -> list[Folder]:
        # Books is only meaningful if EPUBs are being served at all, so it
        # disappears when READWISE_CATEGORIES excludes them rather than showing
        # a list that is always empty.
        views = [SHORT_READS, LONG_READS]
        if BOOK_CATEGORY in self._categories:
            views.append(BOOKS)
        return views

    async def list_view_articles(
        self, view_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        if view_id == BOOKS.id:
            return await self._list_books(cursor)
        if view_id in (SHORT_READS.id, LONG_READS.id):
            return await self.scan_articles(
                lambda a: _reading_time_view(a, short=view_id == SHORT_READS.id),
                VIEW_SOURCE_LOCATIONS,
                cursor,
            )
        raise KeyError(view_id)

    async def _list_books(self, cursor: str | None) -> tuple[list[Article], str | None]:
        """Books come straight from the API's category filter, with no location
        of their own — a book you're part-way through is as much a book as one
        still in Later. That makes this list complete and properly paginated,
        unlike the reading-time views."""
        params: dict[str, str] = {"category": BOOK_CATEGORY}
        if cursor:
            params["pageCursor"] = cursor

        data = await self._get("/list/", params)
        articles = [_article_from_doc(doc) for doc in data.get("results", [])]
        return articles, data.get("nextPageCursor")

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        # The list endpoint filters by a single category, so to surface more
        # than one type (articles + books by default) we fetch the whole
        # location and filter client-side to the configured categories.
        params: dict[str, str] = {"location": folder_id}
        if cursor:
            params["pageCursor"] = cursor

        data = await self._get("/list/", params)
        articles = [
            _article_from_doc(doc)
            for doc in data.get("results", [])
            if doc.get("category") in self._categories
        ]
        return articles, data.get("nextPageCursor")

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        data = await self._get(
            "/list/", {"id": article_id, "withHtmlContent": "true"}
        )

        results = data.get("results", [])
        if not results:
            raise ArticleUnavailable(
                "This article is no longer in your Readwise account.", status=404
            )

        doc = results[0]
        article = _article_from_doc(doc)
        html_content = doc.get("html_content", "")
        if not html_content:
            raise ArticleUnavailable(
                "This item has no readable article text to convert to an EPUB "
                "(it may be a PDF, video, or not yet parsed by Readwise).",
                status=422,
            )
        if _PODCAST_STUB_MARKER in html_content:
            raise ArticleUnavailable(
                "This podcast's transcript isn't ready yet. Open it in Readwise "
                "Reader, load the transcript, then download it again.",
                status=422,
            )

        return article, html_content

    async def close(self):
        await self._client.aclose()
