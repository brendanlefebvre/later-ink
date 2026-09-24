import base64
import json

import httpx
import oauthlib.oauth1  # NB: `import oauthlib` alone does not expose the oauth1 submodule

from .base import ArticleUnavailable


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
