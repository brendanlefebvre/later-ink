import asyncio
import re
import time
from datetime import datetime
from html import unescape

import httpx

from .base import Article, ArticleUnavailable, Connector, Folder, UpstreamError

_TAG_RE = re.compile(r"<[^>]+>")


def _summary_from_content(content: str | None) -> str | None:
    """A short plain-text excerpt for the catalog listing, derived from the
    entry's HTML content (Wallabag has no dedicated excerpt field)."""
    if not content:
        return None
    text = " ".join(unescape(_TAG_RE.sub(" ", content)).split())
    return text[:280] or None

# Wallabag's entry list is filtered by query params, so each "folder" is just a
# preset filter on /api/entries.
FOLDERS = [
    Folder("unread", "Unread", "Unread articles"),
    Folder("starred", "Starred", "Starred articles"),
    Folder("archive", "Archive", "Archived articles"),
    Folder("all", "All", "All saved articles"),
]

_FOLDER_PARAMS: dict[str, dict[str, str]] = {
    "unread": {"archive": "0"},
    "starred": {"starred": "1"},
    "archive": {"archive": "1"},
    "all": {},
}

PER_PAGE = 30
# Refresh a little before the token actually expires to avoid a racing 401.
_TOKEN_MARGIN = 60.0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _article_from_entry(entry: dict) -> Article:
    authors = entry.get("published_by") or []
    author = ", ".join(a for a in authors if a) or None
    kwargs = {
        "id": str(entry["id"]),
        "title": entry.get("title") or "Untitled",
        "author": author,
        "summary": _summary_from_content(entry.get("content")),
        "url": entry.get("url"),
        "language": entry.get("language"),
        "category": "article",
        "image_url": entry.get("preview_picture"),
    }
    updated = _parse_dt(entry.get("updated_at") or entry.get("created_at"))
    if updated is not None:
        kwargs["updated"] = updated
    return Article(**kwargs)


class WallabagConnector(Connector):
    name = "wallabag"
    description = "Wallabag"

    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
    ):
        self._creds = {
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        }
        self._client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30.0)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expiry = 0.0
        self._auth_lock = asyncio.Lock()

    async def _fetch_token(self, payload: dict[str, str]) -> None:
        try:
            resp = await self._client.post("/oauth/v2/token", data=payload)
        except httpx.HTTPError as e:
            raise UpstreamError(f"Could not reach Wallabag: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise UpstreamError(
                f"Wallabag rejected the credentials ({resp.status_code})", resp.status_code
            )
        try:
            data = resp.json()
            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as e:
            raise UpstreamError("Wallabag returned an unexpected auth response") from e
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expiry = time.monotonic() + expires_in - _TOKEN_MARGIN

    async def _ensure_token(self) -> None:
        """Obtain (or refresh) a bearer token, serialized so parallel requests
        don't each kick off their own login."""
        async with self._auth_lock:
            if self._access_token and time.monotonic() < self._expiry:
                return
            base = {
                "client_id": self._creds["client_id"],
                "client_secret": self._creds["client_secret"],
            }
            if self._refresh_token:
                try:
                    await self._fetch_token(
                        {**base, "grant_type": "refresh_token", "refresh_token": self._refresh_token}
                    )
                    return
                except UpstreamError as e:
                    # Only an invalid/expired token justifies a password retry;
                    # re-raise transient failures instead of doubling the calls.
                    if e.status not in (400, 401):
                        raise
                    self._refresh_token = None  # expired/invalid — fall back to password grant
            await self._fetch_token(
                {
                    **base,
                    "grant_type": "password",
                    "username": self._creds["username"],
                    "password": self._creds["password"],
                }
            )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        """Authenticated GET with one 401 re-auth and one 429 retry, and readable errors."""
        params = dict(params or {})
        resp = None
        for attempt in (0, 1):
            await self._ensure_token()
            headers = {"Authorization": f"Bearer {self._access_token}"}
            try:
                resp = await self._client.get(path, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise UpstreamError(f"Could not reach Wallabag: {type(e).__name__}") from e
            if resp.status_code == 401 and attempt == 0:
                self._access_token = None
                self._refresh_token = None  # force a fresh password grant
                continue
            if resp.status_code == 429 and attempt == 0:
                try:
                    delay = min(float(resp.headers.get("Retry-After", "2")), 15.0)
                except ValueError:
                    delay = 2.0
                await asyncio.sleep(delay)
                continue
            break
        if resp.status_code == 401:
            raise UpstreamError("Wallabag rejected the stored credentials", 401)
        if resp.status_code == 429:
            raise UpstreamError("Wallabag is rate-limiting; try again in a minute", 429)
        if resp.status_code >= 400:
            raise UpstreamError(f"Wallabag returned an error ({resp.status_code})", resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise UpstreamError("Wallabag returned an unexpected response") from e

    async def list_folders(self) -> list[Folder]:
        return FOLDERS

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        params = dict(_FOLDER_PARAMS.get(folder_id, {}))
        params.update({"perPage": str(PER_PAGE), "sort": "updated", "order": "desc"})
        if cursor:
            params["page"] = cursor

        data = await self._get("/api/entries.json", params)
        items = (data.get("_embedded") or {}).get("items", [])
        articles = [_article_from_entry(e) for e in items]

        page = data.get("page", 1)
        pages = data.get("pages", 1)
        next_cursor = str(page + 1) if page < pages else None
        return articles, next_cursor

    async def search(
        self, query: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Use Wallabag's native full-text search endpoint (searches content,
        not just the fields we cache). Falls back to the base client-side scan
        on older Wallabag instances that lack /api/search."""
        term = query.strip()
        if not term:
            return [], None

        params = {"term": term, "perPage": str(PER_PAGE)}
        if cursor:
            params["page"] = cursor
        try:
            data = await self._get("/api/search.json", params)
        except UpstreamError as e:
            if e.status == 404:
                return await super().search(query, cursor)
            raise

        items = (data.get("_embedded") or {}).get("items", [])
        articles = [_article_from_entry(e) for e in items]
        page = data.get("page", 1)
        pages = data.get("pages", 1)
        next_cursor = str(page + 1) if page < pages else None
        return articles, next_cursor

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        try:
            data = await self._get(f"/api/entries/{article_id}.json")
        except UpstreamError as e:
            if e.status == 404:
                raise ArticleUnavailable(
                    "This article is no longer in your Wallabag account.", status=404
                ) from e
            raise

        article = _article_from_entry(data)
        html_content = data.get("content") or ""
        if not html_content:
            raise ArticleUnavailable(
                "This item has no readable article text to convert to an EPUB.",
                status=422,
            )
        return article, html_content

    async def close(self):
        await self._client.aclose()
