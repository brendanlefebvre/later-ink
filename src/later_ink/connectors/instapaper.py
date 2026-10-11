import asyncio
import base64
import json

import httpx

from .base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
    decode_json,
    parse_epoch,
    raise_for_upstream,
    retry_after_seconds,
)

BASE_URL = "https://www.instapaper.com/api/2"

# Instapaper's three implicit locations are list "sections" in API v2, not
# folders: GET /folders returns only user-created ones. Their ids here are
# the exact `section` values the API takes, so list_articles can pass them
# straight through. v1's "unread" and "starred" names are gone.
BUILTIN_FOLDERS = [
    Folder("home", "Home", "Bookmarks you haven't archived"),
    Folder("liked", "Liked", "Bookmarks you've liked"),
    Folder("archive", "Archive", "Bookmarks you've archived"),
]
_BUILTIN_IDS = frozenset(f.id for f in BUILTIN_FOLDERS)

# Bookmarks per request. The API allows up to 500; 100 keeps one OPDS page
# proportionate to an e-ink screen and to the scan bounds in base.py.
PAGE_SIZE = 100

# Bookmark.category codes, per the OpenAPI spec. Unknown codes are articles:
# the spec says more may be added and to treat them that way.
_CATEGORIES = {0: "article", 1: "email", 2: "video", 3: "pdf", 4: "social"}


def build_client(
    access_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """The client production uses.

    `transport` exists so one test can drive the real headers and base URL
    through a MockTransport. v1's unit tests all injected a bare client, and
    the bug that broke the live catalog lived in the part they never ran.
    """
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30.0,
        transport=transport,
    )


def _encode_article_id(bookmark_id: str, time: int | None, title: str, url: str | None) -> str:
    """Pack the metadata the parse endpoint cannot return into the article id.

    v2 has no fetch-one-bookmark endpoint, and parse returns the page's title
    but not the bookmark's URL or save time. A bookmark's save `time` never
    changes, so freezing it here keeps content_date (and thus the EPUB's
    dcterms:modified) deterministic without a second fetch. A None time is
    carried as null so the download path reports the same "unknown" the list
    path did. base64url of compact JSON, no padding: URL- and cache-key-safe.
    """
    raw = json.dumps(
        {"i": bookmark_id, "t": time, "n": title, "u": url or ""},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_article_id(token: str) -> tuple[str, int | None, str, str]:
    """Inverse of _encode_article_id, returning (bookmark_id, time, title, url).

    A malformed or forged token is not a server failure, it names an article
    the user cannot have, so it surfaces as ArticleUnavailable(404), not a 500.
    No signature is needed: parse is scoped to the authenticated account, so a
    forged id can only build an EPUB from the requester's own library with
    attacker-chosen metadata on their own download; the url rides only into
    DC.source, never into a request. The bookmark id does ride into the URL
    path, so get_article_html checks it is all digits before using it.
    """
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        time = None if data["t"] is None else int(data["t"])
        return str(data["i"]), time, str(data["n"]), str(data["u"])
    except (ValueError, KeyError, TypeError) as e:
        raise ArticleUnavailable("This article link is invalid.", status=404) from e


def _parse_time(value) -> int | None:
    """A bookmark's save time as an int epoch, or None if missing or malformed.

    Parsed once, here, so the list path and the id-encoded download path derive
    content_date from the same value. An int() that raised would turn one bad
    bookmark into a 500 for the whole folder.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _decode_cursor(cursor: str | None) -> int:
    """The offset a cursor names; 0 if unusable.

    Restart rather than guess, matching base._decode_scan_cursor: a cursor
    from an older release or a hand-edited one must not skip items silently.
    """
    if not cursor:
        return 0
    try:
        offset = int(cursor)
    except ValueError:
        return 0
    return offset if offset >= 0 else 0


def _next_cursor(offset: int, count: int, total) -> str | None:
    """The next page's offset as a cursor, or None when this was the last page.

    A short page always ends the feed, whatever `total` claims, so a library
    that shrinks mid-scan cannot loop. A full page continues unless `total`
    says it was the last one; an unusable `total` is treated as unknown, and
    the short-page rule still terminates the feed one request later.
    """
    if count < PAGE_SIZE:
        return None
    nxt = offset + count
    try:
        total = int(total)
    except (TypeError, ValueError, OverflowError):
        return str(nxt)
    if total >= 0 and nxt >= total:
        return None
    return str(nxt)


def _article_from_bookmark(bm: dict) -> Article:
    bookmark_id = str(bm["id"])
    title = bm.get("title") or "Untitled"
    url = bm.get("url") or None
    time = _parse_time(bm.get("time"))
    code = bm.get("category")
    return Article(
        id=_encode_article_id(bookmark_id, time, title, url),
        title=title,
        author=bm.get("author") or None,
        summary=bm.get("description") or None,
        url=url,
        image_url=bm.get("image") or None,
        category=_CATEGORIES.get(code, "article") if isinstance(code, int) else "article",
        # The list payload carries no word count or language.
        content_date=parse_epoch(time),
    )


def _raise_for_status(resp: httpx.Response) -> None:
    """Map a v2 error status to UpstreamError; return on a 2xx.

    By status only: the spec says the error message may be reworded and must
    not be matched on, and some failures (a bad token, for one) arrive with an
    empty or non-JSON body anyway. 402 and 403 get their own words because a
    self-hoster can cause them: parse credits, or a token from an account
    other than the one that registered the application.
    """
    status = resp.status_code
    if 200 <= status < 300:
        return
    if status == 401:
        raise UpstreamError("Instapaper rejected the stored access token", 401)
    if status == 402:
        raise UpstreamError("Instapaper's article parser quota is exhausted", 402)
    if status == 403:
        raise UpstreamError(
            "Instapaper refused this application; check that the access token was "
            "generated by the account that registered it",
            403,
        )
    raise_for_upstream(resp, "Instapaper")  # 429 and every other 4xx/5xx
    # Only a 3xx reaches here. httpx does not follow redirects, so a sign-in
    # or interstitial page must not be read as a listing or an article.
    raise UpstreamError(f"Instapaper returned an unexpected response ({status})", 502)


class InstapaperConnector(Connector):
    name = "instapaper"
    description = "Instapaper"

    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None):
        # An injected client is taken as-is, including its auth and base URL:
        # the caller that supplies one is a test with a mock transport.
        self._client = client or build_client(access_token)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET with one retry on 429 (honoring Retry-After) and a readable
        transport error. Status and body interpretation is the caller's job."""
        for attempt in (0, 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as e:
                raise UpstreamError(f"Could not reach Instapaper: {type(e).__name__}") from e
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(retry_after_seconds(resp))
                continue
            break
        return resp

    async def _get_object(self, path: str, params: dict[str, str] | None = None) -> dict:
        """GET a JSON object, or raise UpstreamError. Every v2 read returns an
        object; a list or scalar means a proxy or a changed API, not data."""
        resp = await self._get(path, params)
        _raise_for_status(resp)
        payload = decode_json(resp, "Instapaper")
        if not isinstance(payload, dict):
            raise UpstreamError("Instapaper returned an unexpected response")
        return payload

    async def _page(
        self, path: str, params: dict[str, str], cursor: str | None
    ) -> tuple[list[Article], str | None]:
        """One page of bookmarks under `params`, with the next-offset cursor."""
        offset = _decode_cursor(cursor)
        data = await self._get_object(
            path, {**params, "limit": str(PAGE_SIZE), "offset": str(offset)}
        )
        items = data.get("bookmarks")
        if not isinstance(items, list):
            raise UpstreamError("Instapaper returned an unexpected response")
        articles = [
            _article_from_bookmark(bm) for bm in items if isinstance(bm, dict) and "id" in bm
        ]
        # The cursor advances by raw entries, not kept articles: the API's
        # offset counts everything it sent.
        return articles, _next_cursor(offset, len(items), data.get("total"))

    async def list_folders(self) -> list[Folder]:
        data = await self._get_object("folders")
        entries = data.get("folders")
        custom = [
            Folder(str(f["id"]), str(f.get("title") or f["id"]))
            for f in (entries if isinstance(entries, list) else [])
            if isinstance(f, dict) and "id" in f
        ]
        return [*BUILTIN_FOLDERS, *custom]

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        # A built-in is a `section`. A user folder is sent as `folder_id` on
        # its own, which implies section=folder; sending `section` alongside
        # it would make the API ignore folder_id when the two disagree.
        # `since` is never sent: it switches the endpoint into a whole-account
        # change feed.
        if folder_id in _BUILTIN_IDS:
            params = {"section": folder_id}
        else:
            params = {"folder_id": folder_id}
        return await self._page("bookmarks", params, cursor)

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        bookmark_id, time, title, url = _decode_article_id(article_id)
        if not (bookmark_id.isascii() and bookmark_id.isdecimal()):
            # It goes into the URL path; anything but digits is a forged id.
            raise ArticleUnavailable("This article link is invalid.", status=404)
        resp = await self._get(f"bookmarks/{bookmark_id}/parse")
        if resp.status_code == 400:
            # The two documented 400s ("Bookmark not found", "Parsed content
            # unavailable") are told apart only by message, which must not be
            # matched. Either way the item cannot become an EPUB.
            raise ArticleUnavailable(
                "Instapaper has no readable text for this item; it may have "
                "been deleted or never processed.",
                status=422,
            )
        _raise_for_status(resp)
        payload = decode_json(resp, "Instapaper")
        if not isinstance(payload, dict):
            raise UpstreamError("Instapaper returned an unexpected response")
        content = payload.get("content")
        body = content.get("body") if isinstance(content, dict) else None
        if not isinstance(body, str) or not body.strip():
            raise ArticleUnavailable(
                "Instapaper returned no readable text for this article.", status=422
            )
        metadata = payload.get("metadata")
        author = metadata.get("author") if isinstance(metadata, dict) else None
        author_name = author.get("name") if isinstance(author, dict) else None
        # Title, URL and date come from the id, not the parser: the bookmark's
        # title is the one the user sees and may have edited, and the date
        # must not depend on what the parser returns today.
        article = Article(
            id=article_id,
            title=title,
            url=url or None,
            author=author_name or None,
            content_date=parse_epoch(time),
        )
        return article, body

    async def close(self) -> None:
        await self._client.aclose()
