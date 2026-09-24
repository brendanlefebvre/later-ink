import asyncio
import base64
import json

import httpx
import oauthlib.oauth1  # NB: `import oauthlib` alone does not expose the oauth1 submodule

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

BASE_URL = "https://www.instapaper.com/api/1"

# Instapaper's three implicit locations. folders/list returns only user-created
# folders; these are always present and lead the list.
BUILTIN_FOLDERS = [
    Folder("unread", "Unread", "Bookmarks you haven't archived"),
    Folder("starred", "Starred", "Bookmarks you've starred"),
    Folder("archive", "Archive", "Bookmarks you've archived"),
]

# The most-recent items per folder the API will return. There is no forward
# pagination (the `have` parameter is delta-sync, not a cursor), so this is the
# whole reachable window for a folder.
_LIST_LIMIT = "500"


def _encode_article_id(bookmark_id: str, time: int, title: str, url: str | None) -> str:
    """Pack the metadata get_text cannot return into the article id.

    A bookmark's save `time` never changes, so freezing it here keeps
    content_date (and thus the EPUB's dcterms:modified) deterministic without a
    second fetch. base64url of a compact JSON object, no padding — URL- and
    cache-key-safe.
    """
    raw = json.dumps(
        {"i": bookmark_id, "t": time, "n": title, "u": url or ""},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_article_id(token: str) -> tuple[str, int, str, str]:
    """Inverse of _encode_article_id, returning (bookmark_id, time, title, url).

    A malformed or forged token is not a server failure — it names an article
    the user cannot have — so it surfaces as ArticleUnavailable(404), not a 500.
    No signature is needed: get_text is scoped to the authenticated account, so
    a forged id can only ever build an EPUB from the requester's own library
    with attacker-chosen metadata on their own download; the url rides only into
    DC.source, never into a request.
    """
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        return str(data["i"]), int(data["t"]), str(data["n"]), str(data["u"])
    except (ValueError, KeyError, TypeError) as e:
        raise ArticleUnavailable("This article link is invalid.", status=404) from e


def _parse_error_envelope(body: str) -> dict | None:
    """Return the error object if `body` is an Instapaper error envelope, else None.

    Instapaper reports application errors as {"type":"error","error_code":int},
    either as the sole entry of a list or as a bare object — and, for get_text,
    sometimes under HTTP 200. So callers must decide by body content, not status.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("type") == "error":
        return data
    if isinstance(data, list):
        return next((o for o in data if isinstance(o, dict) and o.get("type") == "error"), None)
    return None


def _raise_for_json_error(items: list) -> None:
    """Map an error entry in a bookmarks/list or folders/list array to UpstreamError."""
    err = next((o for o in items if isinstance(o, dict) and o.get("type") == "error"), None)
    if err is None:
        return
    code = err.get("error_code")
    if code == 1040:  # rate-limit exceeded
        raise UpstreamError("Instapaper is rate-limiting this account; try again in a minute", 429)
    if code == 1042:  # application suspended
        raise UpstreamError("Instapaper has suspended this application's API access", 403)
    raise UpstreamError("Instapaper returned an error", 502)


def _raise_for_get_text_error(err: dict) -> None:
    """Map a get_text error envelope. Always raises."""
    code = err.get("error_code")
    if code == 1040:  # rate-limit exceeded
        raise UpstreamError("Instapaper is rate-limiting this account; try again in a minute", 429)
    if code == 1042:  # application suspended
        raise UpstreamError("Instapaper has suspended this application's API access", 403)
    if code == 1241:  # invalid or missing bookmark_id
        raise ArticleUnavailable("This article is no longer in your Instapaper account.", status=404)
    # 1041 premium, 1220/1221 domain restrictions, 1550 text-gen failure, and
    # anything unmapped: the item exists but cannot be turned into an EPUB.
    raise ArticleUnavailable(
        "Instapaper could not produce readable text for this article.", status=422
    )

class _OAuth1Auth(httpx.Auth):
    """Signs each request with OAuth 1.0a HMAC-SHA1.

    Attached to the connector's httpx client as `auth=`, so connector methods
    never build headers themselves and an injected mock client (with no auth)
    is left unsigned — which is fine, mocks do not verify signatures.
    """

    # httpx reads the body inside auth_flow to fold it into the signature base
    # string. For the connector's form-encoded `data=` requests the body is
    # already available; this flag is defensive against a future switch to a
    # streaming body, which would otherwise raise httpx.RequestNotRead.
    requires_request_body = True

    def __init__(self, consumer_key, consumer_secret, oauth_token, oauth_token_secret):
        self._client = oauthlib.oauth1.Client(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=oauth_token,
            resource_owner_secret=oauth_token_secret,
            signature_method=oauthlib.oauth1.SIGNATURE_HMAC_SHA1,
            signature_type=oauthlib.oauth1.SIGNATURE_TYPE_AUTH_HEADER,
        )

    def auth_flow(self, request):
        body = request.content.decode() if request.content else None
        _uri, headers, _body = self._client.sign(
            str(request.url),
            http_method=request.method,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        request.headers["Authorization"] = headers["Authorization"]
        yield request


def _article_from_bookmark(bm: dict) -> Article:
    bookmark_id = str(bm["bookmark_id"])
    title = bm.get("title") or "Untitled"
    url = bm.get("url")
    time = int(bm.get("time") or 0)
    return Article(
        id=_encode_article_id(bookmark_id, time, title, url),
        title=title,
        url=url,
        summary=bm.get("description") or None,
        # Instapaper's list payload carries no author, word count, language,
        # category, or image.
        content_date=parse_epoch(bm.get("time")),
    )


class InstapaperConnector(Connector):
    name = "instapaper"
    description = "Instapaper"

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        oauth_token: str,
        oauth_token_secret: str,
        client: httpx.AsyncClient | None = None,
    ):
        # An injected client is taken as-is (the test seam); it is not signed,
        # which is fine because a MockTransport does not verify signatures.
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30.0,
            auth=_OAuth1Auth(consumer_key, consumer_secret, oauth_token, oauth_token_secret),
        )

    async def _request(self, path: str, data: dict[str, str]) -> httpx.Response:
        """POST with one retry on 429 (honoring Retry-After) and a readable
        transport error. Status/body interpretation is the caller's job."""
        for attempt in (0, 1):
            try:
                resp = await self._client.post(path, data=data)
            except httpx.HTTPError as e:
                raise UpstreamError(f"Could not reach Instapaper: {type(e).__name__}") from e
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(retry_after_seconds(resp))
                continue
            break
        return resp

    async def _post_json(self, path: str, data: dict[str, str]) -> list:
        """For the JSON array endpoints. Guards the top-level shape: decode_json
        is typed Any, and a stray object would make the error scan miss the fault
        and the caller's .get() filter raise AttributeError -> a 500, the exact
        failure class base.decode_json exists to prevent."""
        resp = await self._request(path, data)
        raise_for_upstream(resp, "Instapaper")
        payload = decode_json(resp, "Instapaper")
        if not isinstance(payload, list):
            raise UpstreamError("Instapaper returned an unexpected response")
        _raise_for_json_error(payload)
        return payload

    async def _post_text(self, path: str, data: dict[str, str]) -> str:
        """For bookmarks/get_text. A success body is HTML; a failure body is a
        JSON error envelope, possibly under HTTP 200 — so decide by body first."""
        resp = await self._request(path, data)
        body = resp.text
        err = _parse_error_envelope(body)
        if err is not None:
            _raise_for_get_text_error(err)  # always raises
        raise_for_upstream(resp, "Instapaper")  # HTTP 401/429/4xx/5xx with no envelope
        if not body.strip():
            raise ArticleUnavailable(
                "Instapaper returned no readable text for this article.", status=422
            )
        return body

    async def list_folders(self) -> list[Folder]:
        data = await self._post_json("folders/list", {})
        custom = [
            Folder(str(f["folder_id"]), f.get("title") or str(f["folder_id"]))
            for f in data
            if isinstance(f, dict) and "folder_id" in f
        ]
        return [*BUILTIN_FOLDERS, *custom]

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        # No forward pagination: return the recent window, no cursor. `cursor`
        # is accepted only to satisfy the interface.
        data = await self._post_json(
            "bookmarks/list", {"folder_id": folder_id, "limit": _LIST_LIMIT}
        )
        articles = [
            _article_from_bookmark(o)
            for o in data
            if isinstance(o, dict) and o.get("type") == "bookmark"
        ]
        return articles, None

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        bookmark_id, time, title, url = _decode_article_id(article_id)
        html = await self._post_text("bookmarks/get_text", {"bookmark_id": bookmark_id})
        article = Article(
            id=article_id,
            title=title,
            url=url or None,
            content_date=parse_epoch(time),
        )
        return article, html

    async def close(self) -> None:
        await self._client.aclose()
