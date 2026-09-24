import base64
import json

import httpx
import oauthlib.oauth1  # NB: `import oauthlib` alone does not expose the oauth1 submodule

from .base import ArticleUnavailable, UpstreamError


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
