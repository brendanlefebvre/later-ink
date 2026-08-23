# The Instapaper connector

**Date:** 2026-08-23
**Status:** Approved design, ready for an implementation plan
**Context:** The third connector for later.ink 0.7.0, landing on the
connector-contract groundwork merged in #41.

## Problem

later.ink has two connectors, Readwise and Wallabag, behind the `Connector`
interface, with a conformance contract (`tests/test_connector_contract.py`) that
states what any connector must do. Instapaper is the third. Its Full API differs
from the first two in three ways that shape the whole connector:

1. **Auth is OAuth 1.0a with HMAC-SHA1 request signing**, not a bearer token.
   Every request carries an `Authorization` header signed over the request. This
   is the third auth model the contract design anticipated.
2. **Metadata and content live behind separate endpoints, with no by-id
   metadata fetch.** `bookmarks/list` returns a bookmark's metadata (title, url,
   save time) but not its text; `bookmarks/get_text` returns the article HTML but
   no metadata. There is no endpoint to fetch one bookmark's metadata by id.
   Yet `get_article_html(id)` — the sole source of an EPUB's title and its
   determinism-critical `content_date` — must return both.
3. **No server-side pagination.** `bookmarks/list` returns up to the 500
   most-recent items per folder and stops. The `have` parameter is a
   delta-sync mechanism (it reports deletions), not a backward cursor; paging
   past 500 returns empty. The documented way to reach more is to split a
   library into smaller folders.

## Goals

- A conforming `InstapaperConnector` that passes the existing contract unchanged.
- Preserve the naive-UTC `content_date` invariant the EPUB determinism guarantee
  rests on, sourced from Instapaper's Unix-epoch `time` field.
- Pre-supplied OAuth tokens (no stored password, no runtime token exchange),
  with a one-time helper to mint them.
- Fold in the two connector-coupled follow-ups from the contract branch:
  `close()` on the ABC (#2) and a view/folder id-collision assertion (#1).

## Non-goals

- Runtime xAuth / password storage. Tokens are minted once, out of band.
- Reading-time views (Short/Long reads). Instapaper bookmarks carry no word
  count, so those filters cannot be computed; Instapaper exposes folders only.
- Backward pagination past the API's 500-per-folder ceiling. It is not available.
- Follow-ups #3 (`wallabag._fetch_token`), #4 (`Retry-After` HTTP-date), and #5
  (`test_readwise` `_get` monkeypatch). Deferred; revisit at plan-drafting.
- Changing the `Connector` ABC's three abstract methods.

## Global constants

- **API base URL:** `https://www.instapaper.com/api/1`
- **All calls are POST**, parameters in the request body (form-encoded), per the
  API. OAuth parameters go in the `Authorization` header.
- **New runtime dependency:** `oauthlib>=3.2.2` (the floor clears oauthlib's
  known CVEs). Added to `pyproject.toml`.
- **Env var prefix:** `INSTAPAPER_`.
- Python floor `>=3.11`, ruff `select = ["E4","E7","E9","F","I","UP"]`,
  `target-version = "py311"` — unchanged; line length is not enforced.

---

## 1. Credentials, config, and wiring

The connector receives four pre-supplied credentials plus the standard
injectable client:

```python
def __init__(
    self,
    consumer_key: str,
    consumer_secret: str,
    oauth_token: str,
    oauth_token_secret: str,
    client: httpx.AsyncClient | None = None,
):
```

An injected client is taken as-is (the contract's `MockTransport` seam); the
default client is `httpx.AsyncClient(base_url=BASE_URL, timeout=30.0,
auth=<the OAuth1 signer>)`.

`config.get_instapaper_config()` returns `None` unless **all four** env vars are
set, and otherwise a dict whose keys match `__init__` exactly so
`InstapaperConnector(**cfg)` works, mirroring `get_wallabag_config()`:

| Env var | dict key |
|---|---|
| `INSTAPAPER_CONSUMER_KEY` | `consumer_key` |
| `INSTAPAPER_CONSUMER_SECRET` | `consumer_secret` |
| `INSTAPAPER_OAUTH_TOKEN` | `oauth_token` |
| `INSTAPAPER_OAUTH_TOKEN_SECRET` | `oauth_token_secret` |

Wiring in `main.py`'s `lifespan`, alongside the existing two:

```python
instapaper_cfg = config.get_instapaper_config()
if instapaper_cfg:
    _connectors["instapaper"] = InstapaperConnector(**instapaper_cfg)
```

Instapaper is self-host only (env-configured), like Wallabag. It is not part of
the multi-tenant Readwise path and needs no onboarding-form validation.

## 2. OAuth 1.0a signing

Signing uses **oauthlib** (the pure-Python signing core), wrapped in a small
`httpx.Auth` subclass so it rides on `client.auth` and the connector methods
stay free of header-building:

```python
class _OAuth1Auth(httpx.Auth):
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
        # oauthlib signs over method, full URL, and the form body; it returns the
        # Authorization header to attach. Body params are included in the
        # signature base string, so sign after httpx has encoded the body.
        uri, headers, body = self._client.sign(
            str(request.url),
            http_method=request.method,
            body=request.content.decode() or None,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        request.headers["Authorization"] = headers["Authorization"]
        yield request
```

Because signing is on `client.auth`, an injected `MockTransport` client (no
`auth`) works unchanged — mocks do not verify signatures. The signer gets a unit
test that pins oauthlib's nonce and timestamp (oauthlib accepts both) so the
resulting `Authorization` header is asserted deterministically against a known
value, proving the base-string construction rather than merely "a header exists."

## 3. The composite article id

Instapaper has no by-id metadata fetch, so the connector carries the metadata it
needs *in the article id*. A bookmark's `time` (its save moment → `content_date`)
never changes, so an id that freezes it is deterministic by construction.

```python
def _encode_article_id(bookmark_id: str, time: int, title: str) -> str:
    # base64url of a compact JSON object, no padding — URL- and cache-key-safe.
    raw = json.dumps({"i": bookmark_id, "t": time, "n": title}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

def _decode_article_id(token: str) -> tuple[str, int, str]:
    # A malformed or forged token is not a server failure — it is an article the
    # user cannot have, so it surfaces as ArticleUnavailable(404), not a 500.
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        return str(data["i"]), int(data["t"]), str(data["n"])
    except (ValueError, KeyError, TypeError) as e:
        raise ArticleUnavailable("This article link is invalid.", status=404) from e
```

`list_articles` sets each `Article.id` to `_encode_article_id(...)`.
`get_article_html` decodes it: `bookmark_id` → `get_text`; `time` →
`content_date` (via `base.parse_epoch`); `title` → the `Article.title`.

**No signing on the token is needed.** `get_text` is scoped to the authenticated
account's own tokens, so a forged id can only ever produce an EPUB from the
requester's *own* library, with an attacker-chosen title/date on their *own*
download — no cross-account read, no injection. This reasoning belongs in a
code comment, because a reviewer will ask why the id is not signed.

**Contract interaction.** The Instapaper `ConnectorSpec.article_id` is a valid
encoded token whose `time` equals the contract's expected UTC instant, and whose
`bookmark_id` matches the `get_text` `ok` handler. `content_date` from both
`list_articles` and `get_article_html` then equals that instant, satisfying the
contract's determinism assertions on both paths.

## 4. `base.parse_epoch`

A shared helper beside `parse_dt`, converting Instapaper's integer Unix epoch to
naive UTC — the same normalisation `parse_dt` guarantees, because ebooklib writes
`dcterms:modified` with a literal trailing `Z` and no conversion:

```python
def parse_epoch(value: int | str | None) -> datetime | None:
    """Parse a Unix epoch (seconds) as naive UTC, or None."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
```

## 5. Connector methods

**`list_folders()`** — the three built-in locations as static `Folder`s, plus
the user's custom folders from `folders/list`:

```python
BUILTIN_FOLDERS = [
    Folder("unread", "Unread", "Bookmarks you haven't archived"),
    Folder("starred", "Starred", "Bookmarks you've starred"),
    Folder("archive", "Archive", "Bookmarks you've archived"),
]
```

Custom folders map `folder_id` (a number, stringified) → `Folder(id, title)`.
The built-ins lead, custom folders follow. `folders/list` returns only
user-created folders; the three built-ins are implicit and always present.

**`list_articles(folder_id, cursor)`** — POST `bookmarks/list` with `folder_id`
and `limit=500`. The response is a JSON array of mixed objects; keep those with
`type == "bookmark"`. Build each `Article`:

| Article field | Source |
|---|---|
| `id` | `_encode_article_id(bookmark_id, time, title)` |
| `title` | `title` (or `"Untitled"`) |
| `url` | `url` |
| `summary` | `description` or `None` |
| `content_date` | `parse_epoch(time)` |
| `author`, `word_count`, `language`, `category`, `image_url` | `None` (not in the list payload) |

`cursor` is ignored and `next_cursor` is always `None`: the API has no forward
pagination, so a folder view is its most-recent up-to-500 items as a single
feed. The `cursor` parameter is kept in the signature only to satisfy the
interface.

**`get_article_html(article_id)`** — decode the token; POST `bookmarks/get_text`
with the `bookmark_id`. On HTTP 200 the body is `text/html` (not JSON): return
`resp.text` directly. Build the `Article` from the decoded `time`/`title` (see
§3). An empty body or any error (see §6) raises `ArticleUnavailable`.

**`list_views()`** — inherits the base `[]`. No word count, so no reading-time
views.

**`search(query, cursor)`** — inherits the base client-side scan. Instapaper's
Full API has no search endpoint; the base implementation pages the folders and
filters, bounded by `SEARCH_SCAN_LIMIT`.

**`close()`** — `await self._client.aclose()`.

## 6. Error handling

Instapaper reports application errors as an object in the response array,
`{"type": "error", "error_code": <int>, "message": <str>}`, which can arrive
under HTTP 200. Its `message` is documented as developer-facing and **must not**
be shown to users; the connector supplies its own readable text. A helper runs
after `decode_json` on the JSON endpoints:

```python
def _raise_for_instapaper_error(items: list) -> None:
    err = next((o for o in items if isinstance(o, dict) and o.get("type") == "error"), None)
    if err is None:
        return
    code = err.get("error_code")
    if code == 1040:  # rate-limit exceeded
        raise UpstreamError("Instapaper is rate-limiting this account; try again in a minute", 429)
    if code == 1042:  # application suspended
        raise UpstreamError("Instapaper has suspended this application's API access", 403)
    raise UpstreamError("Instapaper returned an error", 502)
```

Relevant codes (from Instapaper's error table):

| Code | Meaning | Mapping |
|---|---|---|
| 1040 | Rate-limit exceeded | `UpstreamError(429)` |
| 1041 | Premium account required | `get_text`: `ArticleUnavailable(422)`; else `UpstreamError` |
| 1042 | Application suspended | `UpstreamError(403)` |
| 1220 / 1221 | Domain restrictions | `get_text`: `ArticleUnavailable(422)` |
| 1241 | Invalid or missing `bookmark_id` | `get_text`: `ArticleUnavailable(404)` |
| 1242 | Invalid or missing `folder_id` | `list_articles`: `UpstreamError(502)` |
| 1500 | Unexpected service error | `UpstreamError(502)` |
| 1550 | Error generating text version | `get_text`: `ArticleUnavailable(422)` |

**Transport and HTTP-status errors** still go through the shared
`raise_for_upstream(resp, "Instapaper")` (HTTP 401 → rejected credentials,
HTTP 429 → rate-limited, other 4xx/5xx → generic) and the shared
`retry_after_seconds` on a single 429 retry, exactly as the other two
connectors' `_get` loops do. An OAuth signature rejection surfaces as HTTP 401.

**`get_text` error path.** Because `get_text` returns HTML, not JSON, its `_get`
variant checks status first: on HTTP `>= 400`, attempt to parse an error
envelope for the `error_code` and map per the table (defaulting to
`ArticleUnavailable(422)` when the code is unknown or the body is not parseable);
an empty 200 body is `ArticleUnavailable(422)`.

## 7. The one-time mint helper

`src/later_ink/instapaper_auth.py` mints the token pair from a username and
password via the single xAuth call, so the password is never stored by the app:

```python
async def mint_tokens(
    consumer_key: str,
    consumer_secret: str,
    username: str,
    password: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """POST oauth/access_token with x_auth_mode=client_auth; return
    (oauth_token, oauth_token_secret). The response is url-encoded, not JSON."""
```

It signs the request with an oauthlib client holding only the consumer
credentials (no token yet), posts `x_auth_mode=client_auth`,
`x_auth_username`, `x_auth_password`, and parses the url-encoded
`oauth_token` / `oauth_token_secret` from the body. A thin `main()` reads the
consumer key/secret and prompts for username/password, then prints the two
`INSTAPAPER_OAUTH_*` values to paste into the environment. The injectable client
makes it unit-testable with a `MockTransport`. The README gains an Instapaper
setup section covering requesting API access from Instapaper and running the
helper.

## 8. Folded-in follow-ups

**#2 — `close()` on the ABC.** Declare a no-op default on `Connector` so the
method is part of the contract and the runtime guard becomes dead code:

```python
async def close(self) -> None:
    """Release any held resources. No-op by default; connectors with a client override."""
    return None
```

Then `main.py`'s shutdown loop drops its `hasattr` guard:

```python
for c in _connectors.values():
    await c.close()
```

All three concrete connectors already override `close()`.

**#1 — view/folder id collision.** Add a generic assertion to
`tests/test_connector_contract.py`: for each registered connector, no id
returned by `list_views()` may equal any id from `list_folders()`. It is a real
check for Readwise (which has views) and passes trivially for Instapaper and
Wallabag (which have none), guarding the rule `base.py` documents and `main.py`
resolves folder-wins-over-view.

## 9. Testing

**Contract (`tests/test_connector_contract.py`).** Register one
`ConnectorSpec` for Instapaper: a `build` that constructs the connector with an
injected `httpx.AsyncClient(transport=MockTransport(handler))`, an
`_instapaper_handler` factory covering the scenarios `ok`, `missing`,
`error_500`, `unauthorized`, `non_json`, `unreachable`, a `folder_id="unread"`,
and an `article_id` that is a valid encoded token (§3). `get_text` returns HTML
for `ok` and an Instapaper error envelope for `missing`. Adding this spec is what
makes `test_every_shipped_connector_is_registered` pass for the new module.

The `non_json` scenario targets the JSON endpoint path (`list_articles` →
`bookmarks/list` → `decode_json` raises `UpstreamError`). It does **not** apply
to `get_text`, whose HTML body is expected rather than a malformed response:
`get_article_html` reads `resp.text` and never calls `decode_json` on the
article body. The two must not be conflated.

**Connector-specific (`tests/test_instapaper.py`).**

- `_encode_article_id`/`_decode_article_id` round-trip, and malformed/forged
  tokens raise `ArticleUnavailable(404)`.
- `parse_epoch`: an epoch maps to the exact naive-UTC datetime; `None`/invalid → `None`.
- `list_folders` includes the three built-ins and maps custom folders from
  `folders/list`.
- `list_articles` filters to `type == "bookmark"`, maps fields, encodes ids,
  returns `next_cursor is None`.
- `get_article_html` returns the HTML and an `Article` whose `content_date` is
  the decoded epoch as naive UTC.
- The error table: 1241 → `ArticleUnavailable(404)`, 1550/1041 →
  `ArticleUnavailable(422)`, 1040 → `UpstreamError(429)`, an HTTP-200 error
  envelope on `list` → `UpstreamError`.
- `_OAuth1Auth` produces the expected `Authorization` header with a pinned
  nonce/timestamp.

**Mint helper (`tests/test_instapaper_auth.py`).** `mint_tokens` posts the
xAuth params and parses the url-encoded token pair from a mocked response; an
error status raises `UpstreamError`.

**Seam.** `httpx.MockTransport` throughout, per the contract. `pytest tests/ -q`
green; `ruff check src tests` clean.

## 10. Risks

- **Over-fitting the composite id to Instapaper.** It is Instapaper-specific by
  design — the other connectors use bare upstream ids and are unaffected. The
  codec lives in `instapaper.py`, not `base.py`.
- **Instapaper error semantics discovered only against the live API.** The code
  table is from Instapaper's published docs, but a scenario may surface a code
  not mapped here. The default (`ArticleUnavailable(422)` on `get_text`, generic
  `UpstreamError(502)` on the JSON endpoints) fails readably rather than as a
  500, so an unmapped code degrades safely.
- **oauthlib signing correctness.** Mitigated by the pinned-nonce header test
  against a known value, which exercises the real base-string construction.
- **500-per-folder ceiling.** A real product limit, not a bug; documented in the
  README so a user with a larger library understands why older items need a
  folder split.

## 11. Rejected alternatives

| Option | Why not |
|---|---|
| Hand-rolled OAuth1 signing | Reimplements a standard, security-sensitive protocol when an audited signer exists; minimal-deps does not justify it. |
| authlib instead of oauthlib | Its `AsyncOAuth1Client` and token-exchange machinery are dead weight once tokens are pre-supplied, and its client-subclass cuts against the plain injectable-`httpx.AsyncClient` pattern. |
| Runtime xAuth (store username/password) | Stores a password and adds a re-auth path for no gain; pre-supplied tokens are lighter and fit the container-hardening posture. |
| Metadata cache + scan fallback for `get_article_html` | Stateful, lost on restart, and its cold path fans out into multi-folder scans that can fail on aged-out links — reintroducing the "sometimes we have the metadata" fragility determinism removed. |
| Client-side 25-per-page over the capped window | Refetches up to 500 metadata rows per page turn (the connector is stateless), wasteful on e-ink, for a paged feel the API cannot back. |

## 12. Sequencing

`base.parse_epoch` and the ABC `close()` default are prerequisites and land
first. The OAuth signer and the composite-id codec are independent units that
precede the connector methods that use them. The connector's config and `main.py`
wiring can land with the connector. The contract registration and the #1
collision assertion land once the connector exists. The mint helper is
independent and can land any time after the OAuth signer. Readwise and Wallabag
are untouched except for the shared `base.py` additions and the guard removal in
`main.py`.
