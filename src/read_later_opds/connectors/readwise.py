import asyncio
from datetime import UTC, datetime

import httpx

from .base import Article, ArticleUnavailable, Connector, Folder, UpstreamError

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
    Folder("later", "Read Later", "Articles saved for later"),
    Folder("new", "New", "Recently added articles"),
    Folder("shortlist", "Shortlist", "Shortlisted articles"),
    Folder("archive", "Archive", "Archived articles"),
    Folder("feed", "Feed", "Feed articles"),
]

# Readwise categories we attempt to serve as EPUBs. Tweets/youtube/podcasts are
# excluded by default (little or no article text to convert); PDFs too for now.
EPUB_CATEGORIES = ("article", "book")


def _article_from_doc(doc: dict) -> Article:
    return Article(
        id=str(doc["id"]),
        title=doc.get("title") or "Untitled",
        author=doc.get("author"),
        summary=doc.get("summary"),
        url=doc.get("source_url") or doc.get("url"),
        word_count=doc.get("word_count"),
        language=doc.get("language"),
    )


class ReadwiseConnector(Connector):
    name = "readwise"
    description = "Readwise Reader"

    def __init__(self, token: str, categories: tuple[str, ...] = EPUB_CATEGORIES):
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
                "Readwise is rate-limiting this account — try again in a minute", 429
            )
        if resp.status_code == 401:
            raise UpstreamError("Readwise rejected the stored token", 401)
        if resp.status_code >= 400:
            raise UpstreamError(f"Readwise returned an error ({resp.status_code})", resp.status_code)
        return resp.json()

    async def list_folders(self) -> list[Folder]:
        return LOCATIONS

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

        return article, html_content

    async def close(self):
        await self._client.aclose()
