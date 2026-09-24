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
`InstapaperConnector(**cfg)` works, mirroring `get_wallabag_config()` — including
its trimming: read each var with `.strip()` and treat a blank as missing
(`if not all(values.values()): return None`), so whitespace-only config does not
register a broken connector.

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
import oauthlib.oauth1  # `import oauthlib` alone does NOT expose the `oauth1` submodule

class _OAuth1Auth(httpx.Auth):
    # httpx reads the body inside auth_flow to include it in the signature base
    # string. For the connector's form-encoded `data=` requests the body is
    # already available; this flag is defensive — it makes httpx call
    # request.read() first, so a future switch to a streaming body can't break
    # signing with httpx.RequestNotRead.
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
def _encode_article_id(bookmark_id: str, time: int, title: str, url: str | None) -> str:
    # base64url of a compact JSON object, no padding — URL- and cache-key-safe.
    raw = json.dumps(
        {"i": bookmark_id, "t": time, "n": title, "u": url or ""}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

def _decode_article_id(token: str) -> tuple[str, int, str, str]:
    # A malformed or forged token is not a server failure — it is an article the
    # user cannot have, so it surfaces as ArticleUnavailable(404), not a 500.
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        return str(data["i"]), int(data["t"]), str(data["n"]), str(data["u"])
    except (ValueError, KeyError, TypeError) as e:
        raise ArticleUnavailable("This article link is invalid.", status=404) from e
```

`list_articles` sets each `Article.id` to `_encode_article_id(...)`.
`get_article_html` decodes it: `bookmark_id` → `get_text`; `time` →
`content_date` (via `base.parse_epoch`); `title` → the `Article.title`; `url` →
`Article.url`. The `url` is carried so the download path can emit `DC.source` in
the EPUB, matching Readwise and Wallabag — `get_text` returns no metadata, so
without it in the token an Instapaper EPUB would silently drop its source URL.
`DC.source` travels inside the EPUB, so it points back to the original article
even for someone who has only the file and not the OPDS feed it came through —
provenance that survives the file being moved around. An empty string decodes
back to `None` for `Article.url`.

**No signing on the token is needed.** `get_text` is scoped to the authenticated
account's own tokens, so a forged id can only ever produce an EPUB from the
requester's *own* library, with an attacker-chosen title/date/url on their *own*
download — no cross-account read, no injection. (The `url` rides only into
`DC.source` metadata, never into a request.) This reasoning belongs in a code
comment, because a reviewer will ask why the id is not signed.

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
| `id` | `_encode_article_id(bookmark_id, time, title, url)` |
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
`resp.text` directly. Build the `Article` from the decoded `time`/`title`/`url`
(see §3). An empty body raises `ArticleUnavailable`; errors map per §6 (article-level
codes to `ArticleUnavailable`, service and HTTP-status faults to `UpstreamError`).

**`list_views()`** — inherits the base `[]`. No word count, so no reading-time
views.

**`search(query, cursor)`** — inherits the base client-side scan. Instapaper's
Full API has no search endpoint; the base implementation pages the folders and
filters, bounded by `SEARCH_SCAN_LIMIT`.

**`close()`** — `await self._client.aclose()`.

## 6. Error handling

Instapaper reports application errors as an object in the response array,
`{"type": "error", "error_code": <int>, "message": <str>}`, and — this is the
subtlety that shapes both paths — **it can arrive under HTTP 200**. Its
`message` is documented as developer-facing and **must not** be shown to users;
the connector supplies its own readable text.

The connector has two request helpers, named for their POST behaviour (not
`_get`, since every Instapaper call is a POST):

- **`_post_json(path, data) -> list`** — for `bookmarks/list` and
  `folders/list`. After the shared `raise_for_upstream` and `decode_json`, it
  **validates the top-level value is a list** and otherwise raises
  `UpstreamError("Instapaper returned an unexpected response")`. This guard is
  not optional: `decode_json` is typed `Any`, and a stray JSON *object* (a proxy
  fault, or Instapaper wrapping an error) would make the error scan below iterate
  the object's keys, miss the fault, and then let `list_articles`' `.get(...)`
  filter raise `AttributeError` → a 500 — the exact failure class the contract's
  `decode_json` was added to close. With a confirmed list, it scans for an error
  entry:

```python
def _raise_for_json_error(items: list) -> None:
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

- **`_post_text(path, data) -> str`** — for `bookmarks/get_text`. Because a
  success body is HTML and a failure body is a JSON error envelope, it **cannot
  branch on status alone** (the envelope can come with HTTP 200). It decides by
  content: if the body parses as a JSON error envelope, map its `error_code` per
  the table below; otherwise pass the response through `raise_for_upstream`, so
  a non-2xx status with no parseable envelope raises `UpstreamError` (see
  below); on a 2xx return the HTML. An empty 2xx body is
  `ArticleUnavailable(422)`.

  A bare non-2xx is `UpstreamError`, not `ArticleUnavailable`, because with no
  envelope nothing says the fault is the article's: an HTTP 401 means the stored
  tokens were rejected, a 5xx that Instapaper itself is failing. Reporting those
  as "this article can't be converted" would send the reader after the wrong
  problem. Only an envelope naming an article-level code (per the table) makes
  the download `ArticleUnavailable`.

Error-code mapping (from Instapaper's published error table):

| Code | Meaning | `bookmarks/get_text` | `bookmarks/list`, `folders/list` |
|---|---|---|---|
| 1040 | Rate-limit exceeded | `UpstreamError(429)` | `UpstreamError(429)` |
| 1041 | Premium account required | `ArticleUnavailable(422)` | `UpstreamError(502)` |
| 1042 | Application suspended | `UpstreamError(403)` | `UpstreamError(403)` |
| 1220 / 1221 | Domain restrictions | `ArticleUnavailable(422)` | — |
| 1241 | Invalid or missing `bookmark_id` | `ArticleUnavailable(404)` | — |
| 1242 | Invalid or missing `folder_id` | — | `UpstreamError(502)` |
| 1500 | Unexpected service error | `ArticleUnavailable(422)` | `UpstreamError(502)` |
| 1550 | Error generating text version | `ArticleUnavailable(422)` | — |
| (unknown) | anything unmapped | `ArticleUnavailable(422)` | `UpstreamError(502)` |

**Transport and HTTP-status errors** still go through the shared
`raise_for_upstream(resp, "Instapaper")` (HTTP 401 → rejected credentials,
HTTP 429 → rate-limited, other 4xx/5xx → generic) and the shared
`retry_after_seconds` on a single 429 retry, exactly as the other two
connectors' request loops do. An OAuth signature rejection surfaces as HTTP 401.
This applies to `_post_text` as well as `_post_json`. For `_post_text`, the
JSON-envelope check runs before `raise_for_upstream`, so an application error
envelope yields its mapped error from the table (for example
`ArticleUnavailable(404)` for 1241 under HTTP 400) rather than a generic
HTTP-status `UpstreamError`.

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
`ConnectorSpec` for Instapaper. Its `build` must match the existing builders'
shape exactly — it returns a **`(connector, client)` tuple**, and the injected
client is given a `base_url` (the current `ConnectorSpec.build` is typed
`Callable[..., tuple[Connector, httpx.AsyncClient]]`, and
`test_close_releases_the_http_client_and_is_safe_to_call_twice` does
`conn, client = spec.build(...)` then asserts `client.is_closed`; a bare-connector
`build` breaks unpacking for the whole parametrized suite, and a client with no
`base_url` makes `client.post("bookmarks/list")` raise `ValueError: unknown url
type` before the mock is reached):

```python
def _build_instapaper(handler: Callable) -> tuple[Connector, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
    )
    connector = InstapaperConnector(
        consumer_key="ck", consumer_secret="cs",
        oauth_token="ot", oauth_token_secret="ots",
        client=client,
    )
    return connector, client
```

An `_instapaper_handler` factory covers the scenarios `ok`, `missing`,
`error_500`, `unauthorized`, `non_json`, `unreachable`, with `folder_id="unread"`
and an `article_id` that is a valid encoded token whose `time` is the contract
epoch `1735779845` (the Unix epoch of the contract's
`CONTENT_DATE_UTC = datetime(2025, 1, 2, 1, 4, 5)`; use the same value for the
`bookmarks/list` `ok` handler's `time` field so both the list and download paths
land on `CONTENT_DATE_UTC`). `get_text` returns HTML for `ok`; for `missing` it returns
**an Instapaper error envelope with error_code 1241 under HTTP 400** (the pinned
status the `missing` scenario asserts against → `ArticleUnavailable(404)`).
Adding this spec is what makes `test_every_shipped_connector_is_registered` pass
for the new module.

The `non_json` scenario targets the JSON endpoint path (`list_articles` →
`bookmarks/list` → `decode_json` raises `UpstreamError`). It does **not** apply
to `get_text`, whose HTML body is expected rather than a malformed response:
`get_article_html` reads `resp.text` and never calls `decode_json` on the
article body. The two must not be conflated.

**Connector-specific (`tests/test_instapaper.py`).**

- `_encode_article_id`/`_decode_article_id` round-trip (including `url`, and an
  empty `url` decoding back to `None`), and malformed/forged tokens raise
  `ArticleUnavailable(404)`.
- `parse_epoch`: an epoch maps to the exact naive-UTC datetime; `None`/invalid → `None`.
- `list_folders` includes the three built-ins and maps custom folders from
  `folders/list`.
- `list_articles` filters to `type == "bookmark"`, maps fields, encodes ids,
  returns `next_cursor is None`.
- **Shape guard:** a `bookmarks/list` response that is a JSON *object* (not a
  list) raises `UpstreamError`, not `AttributeError`/500 (finding 11).
- `get_article_html` returns the HTML and an `Article` whose `content_date` is
  the decoded epoch as naive UTC and whose `url` is the decoded url (so
  `DC.source` is populated on the EPUB path).
- The error table on **both** paths: for `get_text`, 1241 →
  `ArticleUnavailable(404)` and 1550/1041/1220 → `ArticleUnavailable(422)`,
  including when the envelope arrives under **HTTP 200**; for `list`, 1040 →
  `UpstreamError(429)` and an HTTP-200 error envelope → `UpstreamError`.
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

## 13. Integration checklist

Cross-cutting touch-points that are not part of the connector's own code but
will break the build, the suite, or hermeticity if missed. Surfaced by the
supplemental review; each is confirmed against the current tree.

- **Lockfiles, not just `pyproject.toml`.** CI installs `requirements-dev.txt`
  and the Docker image installs `requirements.txt`, both `--require-hashes`, and
  the project itself installs `--no-deps`. Adding `oauthlib>=3.2.2` to
  `pyproject.toml` alone leaves CI and the image unable to import it. Regenerate
  both locks with the `uv pip compile … --generate-hashes` command recorded at
  the top of each file (`requirements.txt` and `requirements-dev.txt`);
  `requirements-build.txt` is unaffected.
- **Test hermeticity.** Extend `_CONNECTOR_ENV` in `tests/conftest.py` (the
  autouse fixture that unsets connector env vars) with the four `INSTAPAPER_*`
  vars, or the suite is non-hermetic on any machine that has them set.
- **`.env.example`.** Add a commented Instapaper section (the four
  `INSTAPAPER_*` vars) beside the existing Readwise/Wallabag block, matching the
  README's setup section.
- **Docs.** README gains the Instapaper setup section: requesting Full API
  access from Instapaper, running the mint helper, and the 500-per-folder
  ceiling with the folder-split workaround.
