# Instapaper Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third read-it-later connector, Instapaper, behind the existing `Connector` interface, passing the connector conformance contract unchanged.

**Architecture:** A new `InstapaperConnector` speaks Instapaper's Full API (OAuth 1.0a HMAC-SHA1 signing via `oauthlib`, pre-supplied tokens). Because Instapaper's `bookmarks/get_text` returns HTML with no metadata and there is no by-id metadata endpoint, each `Article.id` is a base64url "composite id" carrying `bookmark_id`, save-`time`, `title`, and `url`, so `get_article_html` reconstructs metadata deterministically without a second fetch. Shared helpers (`parse_epoch`, an ABC `close()` default) land in `base.py`; the connector is self-host-only config like Wallabag.

**Tech Stack:** Python ≥3.11, `httpx` (async, `MockTransport` test seam), `oauthlib` (OAuth1 signing), `pytest`/`pytest-asyncio`, `ebooklib` (downstream EPUB), `hatchling` (build).

**Spec:** `docs/superpowers/specs/2026-08-23-instapaper-connector-design.md` (as amended by `docs/superpowers/specs/2026-08-23-instapaper-connector-design-supplemental-review.md`). The plan argues from the spec; executors should read both.

## Global Constraints

- **API base URL:** `https://www.instapaper.com/api/1`. Every Instapaper call is **POST** with parameters form-encoded in the body (`data={...}`); OAuth parameters go in the `Authorization` header.
- **New runtime dependency:** `oauthlib>=3.2.2`. Adding it to `pyproject.toml` is **not sufficient** — CI installs `requirements-dev.txt` and the Docker image installs `requirements.txt`, both with `--require-hashes`, and the project installs `--no-deps`. Both lockfiles must be regenerated (Task 3).
- **Env var prefix:** `INSTAPAPER_`. Four vars, all required together: `INSTAPAPER_CONSUMER_KEY`, `INSTAPAPER_CONSUMER_SECRET`, `INSTAPAPER_OAUTH_TOKEN`, `INSTAPAPER_OAUTH_TOKEN_SECRET`.
- **Determinism invariant:** every `Article.content_date` a connector produces must be a **naive UTC** `datetime` (or `None`). Instapaper's `time` is an integer Unix epoch; convert it with `base.parse_epoch`. A tz-aware value silently corrupts EPUB `dcterms:modified`.
- **Instapaper error `message` strings are developer-facing and must never be shown to users.** The connector supplies its own readable text.
- **Python floor `>=3.11`; ruff `select = ["E4","E7","E9","F","I","UP"]`, `target-version = "py311"`.** Line length is **not** enforced. Run `ruff check src tests` clean before every commit.
- **The full suite must stay green after every task** (`pytest tests/ -q`). In particular, `tests/test_connector_contract.py::test_every_shipped_connector_is_registered` walks the `later_ink.connectors` package and fails if a concrete `Connector` subclass is unregistered — so the task that introduces `class InstapaperConnector(Connector)` must also register it (Task 6).

## Task ordering constraints (read before starting)

- **Tasks 1–2** (base.py additions) are prerequisites for later tasks and are independent of each other.
- **Task 3** (add `oauthlib` + regenerate lockfiles) must complete before any code that imports `oauthlib` can run in a fresh environment (Tasks 3, 6, 8).
- **Tasks 4 and 5** (codec, error helpers) are module-level functions in `instapaper.py` with no concrete `Connector` subclass, so they do not trip the registration test. They must precede Task 6, which uses them.
- **Task 6** introduces the concrete connector class **and** registers it in the contract in the same commit — do not split these, or the suite goes red between commits.
- **Task 7** (config + `main.py` wiring + `conftest` isolation) must update `tests/conftest.py` in the same commit as the `main.py` wiring, or app-startup tests become non-hermetic on machines with `INSTAPAPER_*` set.
- **Task 8** (mint helper + README) is independent once Task 3 has landed `oauthlib`.

---

### Task 1: `base.parse_epoch`

**Files:**
- Modify: `src/later_ink/connectors/base.py` (add a function beside `parse_dt`)
- Test: `tests/test_base_helpers.py` (create)

**Interfaces:**
- Consumes: nothing new (`base.py` already imports `from datetime import UTC, datetime`).
- Produces: `parse_epoch(value: int | str | None) -> datetime | None` — a Unix epoch (seconds) as a **naive UTC** datetime, or `None` for missing/invalid input.

- [ ] **Step 1: Write the failing test**

Create `tests/test_base_helpers.py`:

```python
from datetime import datetime

from later_ink.connectors.base import parse_epoch


def test_parse_epoch_returns_naive_utc():
    # 1735779845 == 2025-01-02T01:04:05Z
    result = parse_epoch(1735779845)
    assert result == datetime(2025, 1, 2, 1, 4, 5)
    assert result.tzinfo is None


def test_parse_epoch_accepts_numeric_string():
    assert parse_epoch("1735779845") == datetime(2025, 1, 2, 1, 4, 5)


def test_parse_epoch_none_and_blank_and_garbage_return_none():
    assert parse_epoch(None) is None
    assert parse_epoch("") is None
    assert parse_epoch("not-a-number") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_base_helpers.py -v`
Expected: FAIL with `ImportError` / `cannot import name 'parse_epoch'`.

- [ ] **Step 3: Write minimal implementation**

In `src/later_ink/connectors/base.py`, add directly below `parse_dt` (after its closing line, before `retry_after_seconds`):

```python
def parse_epoch(value: int | str | None) -> datetime | None:
    """Parse a Unix epoch (seconds) as naive UTC, or None.

    Instapaper timestamps are integer epochs, not ISO strings, so parse_dt
    does not apply. Normalized to naive UTC for the same reason parse_dt is:
    ebooklib writes dcterms:modified with a literal trailing Z and no
    conversion, so a tz-aware value would be mislabelled.
    """
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_base_helpers.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/connectors/base.py tests/test_base_helpers.py
git commit -m "Add base.parse_epoch for Unix-epoch content dates"
```

---

### Task 2: `Connector.close()` default + remove the `main.py` guard

**Files:**
- Modify: `src/later_ink/connectors/base.py` (add a `close` method to the `Connector` ABC)
- Modify: `src/later_ink/main.py` (drop the `hasattr` guard in the shutdown loop)
- Test: `tests/test_base_helpers.py` (append)

**Interfaces:**
- Consumes: the `Connector` ABC (`base.py`).
- Produces: `Connector.close(self) -> None` — an awaitable no-op default; concrete connectors override it.

**Why:** the contract already requires every connector to have an idempotent `close()`, `main.py` guards the call with `hasattr`, and the ABC never declared it. Declaring a default makes the guard dead code (spec §8, follow-up #2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_base_helpers.py`:

```python
import asyncio

from later_ink.connectors.base import Article, Connector, Folder


class _BareConnector(Connector):
    """A connector that does NOT override close(), to prove the ABC default."""

    name = "bare"
    description = "Bare"

    async def list_folders(self) -> list[Folder]:
        return []

    async def list_articles(self, folder_id, cursor=None):
        return [], None

    async def get_article_html(self, article_id):
        return Article(id=article_id, title="x"), "<p>x</p>"


def test_connector_close_default_is_a_noop_awaitable():
    async def go():
        conn = _BareConnector()
        assert await conn.close() is None
        await conn.close()  # idempotent

    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_base_helpers.py::test_connector_close_default_is_a_noop_awaitable -v`
Expected: FAIL — instantiating `_BareConnector` raises `TypeError: Can't instantiate abstract class` is NOT expected (close is not abstract); instead the failure is `AttributeError: 'super' object has no attribute 'close'` or `'_BareConnector' object has no attribute 'close'` when `close()` is called. (If instead it errors that `_BareConnector` is abstract, that means an abstract method was left unimplemented — recheck the three methods above.)

- [ ] **Step 3: Add the ABC default**

In `src/later_ink/connectors/base.py`, inside `class Connector(ABC)`, add after `get_article_html`'s definition and before `list_views`:

```python
    async def close(self) -> None:
        """Release any held resources. No-op by default; connectors that build
        an httpx client override this to close it. Declared here so callers can
        invoke close() on any Connector without a hasattr guard."""
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_base_helpers.py -v`
Expected: PASS (all tests, including the new one).

- [ ] **Step 5: Remove the now-dead guard in `main.py`**

Find this block in `src/later_ink/main.py` (in `lifespan`, shutdown path):

```python
    for c in _connectors.values():
        if hasattr(c, "close"):
            await c.close()
```

Replace it with:

```python
    for c in _connectors.values():
        await c.close()
```

- [ ] **Step 6: Run the app/shutdown tests to verify no regression**

Run: `pytest tests/test_app.py tests/test_connector_contract.py -q`
Expected: PASS (unchanged counts; the shutdown path still closes every connector).

- [ ] **Step 7: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/connectors/base.py src/later_ink/main.py tests/test_base_helpers.py
git commit -m "Declare Connector.close() default; drop the hasattr guard"
```

---

### Task 3: Add `oauthlib`, regenerate lockfiles, and the `_OAuth1Auth` signer

**Files:**
- Modify: `pyproject.toml` (add `oauthlib>=3.2.2` to `[project].dependencies`)
- Regenerate: `requirements.txt`, `requirements-dev.txt` (hashed locks)
- Create: `src/later_ink/connectors/instapaper.py` (module with the signer only, for now)
- Test: `tests/test_instapaper.py` (create)

**Interfaces:**
- Consumes: `oauthlib.oauth1`, `httpx.Auth`.
- Produces: `_OAuth1Auth(consumer_key, consumer_secret, oauth_token, oauth_token_secret)` — an `httpx.Auth` that adds an OAuth 1.0a HMAC-SHA1 `Authorization` header signed over method, URL, and form body.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to the `[project]` `dependencies` list (place it beside the other runtime deps, e.g. after `lxml`; exact position is not significant):

```toml
    "oauthlib>=3.2.2",
```

- [ ] **Step 2: Regenerate both hashed lockfiles**

The compile command is recorded at the top of each lockfile. Run both (requires `uv`):

```bash
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.11 --output-file requirements.txt
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --python-version 3.11 --output-file requirements-dev.txt
```

Then install the dev lock into the working environment so tests can import `oauthlib`:

```bash
python -m pip install --require-hashes -r requirements-dev.txt
```

- [ ] **Step 3: Verify `oauthlib` is now importable**

Run: `python -c "import oauthlib.oauth1; print(oauthlib.oauth1.SIGNATURE_HMAC_SHA1)"`
Expected: prints `HMAC-SHA1` (no `ModuleNotFoundError`, no `AttributeError`). This must **pass**, not be skipped — a failure here means the lock was not regenerated or not installed, and every later oauthlib-using task will fail.

- [ ] **Step 4: Write the failing test**

Create `tests/test_instapaper.py`:

```python
import httpx

from later_ink.connectors.instapaper import _OAuth1Auth


def test_oauth1_auth_signs_with_pinned_nonce_and_timestamp():
    auth = _OAuth1Auth("ck", "cs", "ot", "ots")
    # Pin nonce/timestamp so the signature is deterministic and we assert the
    # real base-string construction, not merely "a header exists".
    auth._client.nonce = "pinned-nonce"
    auth._client.timestamp = "1700000000"

    request = httpx.Request(
        "POST",
        "https://www.instapaper.com/api/1/bookmarks/list",
        data={"folder_id": "unread", "limit": "500"},
    )
    flow = auth.auth_flow(request)
    signed = next(flow)

    header = signed.headers["Authorization"]
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in header
    assert 'oauth_token="ot"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_nonce="pinned-nonce"' in header
    assert 'oauth_timestamp="1700000000"' in header
    assert "oauth_signature=" in header
```

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest tests/test_instapaper.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'later_ink.connectors.instapaper'`.

- [ ] **Step 6: Write the module with the signer**

Create `src/later_ink/connectors/instapaper.py`:

```python
import oauthlib.oauth1  # NB: `import oauthlib` alone does not expose the oauth1 submodule

import httpx


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
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_instapaper.py -v`
Expected: PASS.

- [ ] **Step 8: Lint and commit**

```bash
ruff check src tests
git add pyproject.toml requirements.txt requirements-dev.txt src/later_ink/connectors/instapaper.py tests/test_instapaper.py
git commit -m "Add oauthlib and the Instapaper OAuth1 request signer"
```

---

### Task 4: The composite article-id codec

**Files:**
- Modify: `src/later_ink/connectors/instapaper.py` (add codec functions)
- Test: `tests/test_instapaper.py` (append)

**Interfaces:**
- Consumes: `ArticleUnavailable` from `base`.
- Produces:
  - `_encode_article_id(bookmark_id: str, time: int, title: str, url: str | None) -> str`
  - `_decode_article_id(token: str) -> tuple[str, int, str, str]` — returns `(bookmark_id, time, title, url)`; raises `ArticleUnavailable(status=404)` on a malformed/forged token. An empty-string url is preserved as `""` (the caller maps `""` back to `None`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instapaper.py`:

```python
import pytest

from later_ink.connectors.base import ArticleUnavailable
from later_ink.connectors.instapaper import _decode_article_id, _encode_article_id


def test_article_id_round_trips():
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "https://example.com/a")


def test_article_id_round_trips_empty_url():
    token = _encode_article_id("42", 1735779845, "Some Title", None)
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "")


def test_article_id_token_is_url_path_safe():
    token = _encode_article_id("42", 1735779845, "Café — déjà vu", "https://example.com/a?x=1")
    # base64url alphabet only (plus no padding); safe to drop into a URL path.
    assert all(c.isalnum() or c in "-_" for c in token)


def test_decode_rejects_malformed_token():
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id("!!!not-base64!!!")
    assert exc.value.status == 404


def test_decode_rejects_wellformed_base64_missing_fields():
    import base64
    import json

    bad = base64.urlsafe_b64encode(json.dumps({"i": "42"}).encode()).decode().rstrip("=")
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id(bad)
    assert exc.value.status == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_instapaper.py -k article_id -v`
Expected: FAIL with `ImportError` / `cannot import name '_encode_article_id'`.

- [ ] **Step 3: Write the codec**

In `src/later_ink/connectors/instapaper.py`, add the stdlib imports at the top (with the existing imports) and the functions below the imports:

```python
import base64
import json

from .base import ArticleUnavailable
```

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_instapaper.py -k article_id -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/connectors/instapaper.py tests/test_instapaper.py
git commit -m "Add the Instapaper composite article-id codec"
```

---

### Task 5: Instapaper error-mapping helpers

**Files:**
- Modify: `src/later_ink/connectors/instapaper.py` (add error helpers + envelope parser)
- Test: `tests/test_instapaper.py` (append)

**Interfaces:**
- Consumes: `UpstreamError`, `ArticleUnavailable` from `base`.
- Produces:
  - `_parse_error_envelope(body: str) -> dict | None` — returns the error object (`{"type":"error","error_code":int,...}`) if `body` is a JSON error envelope (a list containing one, or a bare error object), else `None`.
  - `_raise_for_json_error(items: list) -> None` — for `bookmarks/list` / `folders/list`. Raises `UpstreamError` if `items` contains an error entry; returns otherwise.
  - `_raise_for_get_text_error(err: dict) -> None` — for `bookmarks/get_text`. Always raises: `ArticleUnavailable` for content/availability codes, `UpstreamError` for service/rate codes.

Error-code mapping (spec §6): 1040 → rate-limit; 1041 premium; 1042 suspended; 1220/1221 domain; 1241 bad bookmark_id; 1242 bad folder_id; 1500 service; 1550 text-gen failure.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instapaper.py`:

```python
from later_ink.connectors.base import UpstreamError
from later_ink.connectors.instapaper import (
    _parse_error_envelope,
    _raise_for_get_text_error,
    _raise_for_json_error,
)


def test_parse_error_envelope_detects_list_and_bare_forms():
    assert _parse_error_envelope('[{"type":"error","error_code":1241}]')["error_code"] == 1241
    assert _parse_error_envelope('{"type":"error","error_code":1550}')["error_code"] == 1550


def test_parse_error_envelope_returns_none_for_html_and_non_error_json():
    assert _parse_error_envelope("<article><p>Body</p></article>") is None
    assert _parse_error_envelope('[{"type":"bookmark","bookmark_id":1}]') is None


def test_raise_for_json_error_maps_rate_limit_and_suspension():
    with pytest.raises(UpstreamError) as e1:
        _raise_for_json_error([{"type": "error", "error_code": 1040}])
    assert e1.value.status == 429
    with pytest.raises(UpstreamError) as e2:
        _raise_for_json_error([{"type": "error", "error_code": 1042}])
    assert e2.value.status == 403


def test_raise_for_json_error_passes_clean_list():
    assert _raise_for_json_error([{"type": "bookmark", "bookmark_id": 1}]) is None


def test_raise_for_json_error_unknown_code_is_502():
    with pytest.raises(UpstreamError) as e:
        _raise_for_json_error([{"type": "error", "error_code": 1500}])
    assert e.value.status == 502


def test_raise_for_get_text_error_maps_codes():
    with pytest.raises(ArticleUnavailable) as e_missing:
        _raise_for_get_text_error({"type": "error", "error_code": 1241})
    assert e_missing.value.status == 404

    for code in (1041, 1220, 1221, 1550):
        with pytest.raises(ArticleUnavailable) as e:
            _raise_for_get_text_error({"type": "error", "error_code": code})
        assert e.value.status == 422

    with pytest.raises(UpstreamError) as e_rate:
        _raise_for_get_text_error({"type": "error", "error_code": 1040})
    assert e_rate.value.status == 429


def test_raise_for_get_text_error_unknown_code_is_article_unavailable_422():
    with pytest.raises(ArticleUnavailable) as e:
        _raise_for_get_text_error({"type": "error", "error_code": 9999})
    assert e.value.status == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_instapaper.py -k "error" -v`
Expected: FAIL with `ImportError` on the new names.

- [ ] **Step 3: Write the helpers**

In `src/later_ink/connectors/instapaper.py`, extend the `from .base import ...` line to include `UpstreamError`:

```python
from .base import ArticleUnavailable, UpstreamError
```

Then add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_instapaper.py -k "error" -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/connectors/instapaper.py tests/test_instapaper.py
git commit -m "Add Instapaper error-envelope parsing and code mapping"
```

---

### Task 6: The `InstapaperConnector` class + contract registration

**Files:**
- Modify: `src/later_ink/connectors/instapaper.py` (add `BUILTIN_FOLDERS`, `BASE_URL`, `_article_from_bookmark`, the class)
- Modify: `tests/test_connector_contract.py` (register the spec; add the #1 collision assertion)
- Test: `tests/test_instapaper.py` (append connector-behaviour tests)

**Interfaces:**
- Consumes: `_OAuth1Auth`, `_encode_article_id`/`_decode_article_id`, `_parse_error_envelope`/`_raise_for_json_error`/`_raise_for_get_text_error` (this module); `Article`, `Folder`, `Connector`, `ArticleUnavailable`, `UpstreamError`, `parse_epoch`, `raise_for_upstream`, `decode_json`, `retry_after_seconds` (base).
- Produces: `InstapaperConnector(consumer_key, consumer_secret, oauth_token, oauth_token_secret, client: httpx.AsyncClient | None = None)` with `name = "instapaper"`; methods `list_folders`, `list_articles`, `get_article_html`, `close`; inherits `list_views` (`[]`) and `search` (base scan).

**Ordering:** introduce the class and its `ConnectorSpec` in the **same commit** — a concrete `Connector` subclass with no spec fails `test_every_shipped_connector_is_registered`.

- [ ] **Step 1: Write the failing connector tests**

Append to `tests/test_instapaper.py`:

```python
import asyncio
from datetime import datetime

from later_ink.connectors.instapaper import InstapaperConnector

_BOOKMARK = {
    "type": "bookmark",
    "bookmark_id": 42,
    "title": "Some Title",
    "url": "https://example.com/a",
    "description": "An excerpt",
    "time": 1735779845,
}


def _conn(handler):
    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
    )
    return InstapaperConnector("ck", "cs", "ot", "ots", client=client), client


def test_list_folders_has_builtins_then_custom():
    def handler(request):
        assert request.url.path.endswith("/folders/list")
        return httpx.Response(200, json=[{"folder_id": 100, "title": "Recipes"}])

    conn, client = _conn(handler)

    async def go():
        folders = await conn.list_folders()
        await conn.close()
        return folders

    folders = asyncio.run(go())
    assert [f.id for f in folders] == ["unread", "starred", "archive", "100"]
    assert folders[-1].title == "Recipes"


def test_list_articles_maps_bookmark_and_has_no_cursor():
    def handler(request):
        assert request.url.path.endswith("/bookmarks/list")
        return httpx.Response(200, json=[{"type": "user", "user_id": 1}, _BOOKMARK])

    conn, client = _conn(handler)

    async def go():
        result = await conn.list_articles("unread")
        await conn.close()
        return result

    articles, cursor = asyncio.run(go())
    assert cursor is None
    assert len(articles) == 1
    a = articles[0]
    assert a.title == "Some Title"
    assert a.url == "https://example.com/a"
    assert a.summary == "An excerpt"
    assert a.content_date == datetime(2025, 1, 2, 1, 4, 5)


def test_list_articles_rejects_non_list_shape():
    def handler(request):
        return httpx.Response(200, json={"type": "error", "error_code": 1500})

    conn, client = _conn(handler)

    async def go():
        try:
            await conn.list_articles("unread")
        finally:
            await conn.close()

    with pytest.raises(UpstreamError):
        asyncio.run(go())


def test_get_article_html_returns_html_and_reconstructs_metadata():
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")

    def handler(request):
        assert request.url.path.endswith("/bookmarks/get_text")
        return httpx.Response(200, text="<article><p>Body</p></article>")

    conn, client = _conn(handler)

    async def go():
        result = await conn.get_article_html(token)
        await conn.close()
        return result

    article, html = asyncio.run(go())
    assert "<p>Body</p>" in html
    assert article.title == "Some Title"
    assert article.url == "https://example.com/a"
    assert article.content_date == datetime(2025, 1, 2, 1, 4, 5)


def test_get_article_html_maps_error_envelope_under_http_200():
    token = _encode_article_id("42", 1735779845, "t", "u")

    def handler(request):
        # An error envelope arriving under HTTP 200 must still be treated as an error.
        return httpx.Response(200, json=[{"type": "error", "error_code": 1241}])

    conn, client = _conn(handler)

    async def go():
        try:
            await conn.get_article_html(token)
        finally:
            await conn.close()

    with pytest.raises(ArticleUnavailable) as e:
        asyncio.run(go())
    assert e.value.status == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_instapaper.py -k "list_ or get_article" -v`
Expected: FAIL with `ImportError: cannot import name 'InstapaperConnector'`.

- [ ] **Step 3: Implement the connector**

In `src/later_ink/connectors/instapaper.py`: extend the imports and add the constants, mapping helper, and class. First, extend the `from .base import ...` line to its full set, and add `asyncio`:

```python
import asyncio

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
```

Then add:

```python
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
```

- [ ] **Step 4: Run the connector tests to verify they pass**

Run: `pytest tests/test_instapaper.py -v`
Expected: PASS (all, including Tasks 3–5 tests).

- [ ] **Step 5: Register the connector in the contract**

In `tests/test_connector_contract.py`, add the imports and builder, then the spec. Add near the other builders:

```python
from later_ink.connectors.instapaper import InstapaperConnector, _encode_article_id

FOLDER_ID_INSTAPAPER = "unread"
# time 1735779845 == CONTENT_DATE_UTC (2025-01-02T01:04:05Z), so both the list
# and download paths land on the same determinism assertion.
_INSTAPAPER_TIME = 1735779845
ARTICLE_ID_INSTAPAPER = _encode_article_id("42", _INSTAPAPER_TIME, "Some Title", "https://ex.com/a")


def _build_instapaper(handler):
    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
    )
    connector = InstapaperConnector("ck", "cs", "ot", "ots", client=client)
    return connector, client


def _instapaper_handler(scenario):
    bookmark = {
        "type": "bookmark",
        "bookmark_id": 42,
        "title": "Some Title",
        "url": "https://ex.com/a",
        "description": "An excerpt",
        "time": _INSTAPAPER_TIME,
    }

    def handler(request):
        path = request.url.path
        if scenario == "unreachable":
            raise httpx.ConnectError("boom")
        if scenario == "error_500":
            return httpx.Response(500, text="upstream is broken")
        if scenario == "unauthorized":
            return httpx.Response(401, text="")
        if scenario == "non_json" and path.endswith("/bookmarks/list"):
            return httpx.Response(200, text="<html>not json</html>")
        if path.endswith("/folders/list"):
            return httpx.Response(200, json=[{"folder_id": 100, "title": "Recipes"}])
        if path.endswith("/bookmarks/list"):
            return httpx.Response(200, json=[{"type": "user", "user_id": 1}, bookmark])
        if path.endswith("/bookmarks/get_text"):
            if scenario == "missing":
                return httpx.Response(400, json=[{"type": "error", "error_code": 1241}])
            return httpx.Response(200, text="<article><p>Body</p></article>")
        return httpx.Response(200, json=[])

    return handler
```

Then add a `ConnectorSpec` to the `SPECS` list:

```python
    ConnectorSpec(
        label="instapaper",
        cls=InstapaperConnector,
        build=_build_instapaper,
        handlers=_instapaper_handler,
        folder_id=FOLDER_ID_INSTAPAPER,
        article_id=ARTICLE_ID_INSTAPAPER,
    ),
```

- [ ] **Step 6: Add the #1 view/folder collision assertion**

In `tests/test_connector_contract.py`, add a parametrized test (it runs against every registered connector via the existing `spec` fixture):

```python
def test_view_ids_do_not_collide_with_folder_ids(spec):
    async def go():
        conn, client = spec.build(spec.handlers("ok"))
        try:
            folder_ids = {f.id for f in await conn.list_folders()}
            view_ids = {v.id for v in await conn.list_views()}
        finally:
            await conn.close()
        return folder_ids, view_ids

    folder_ids, view_ids = asyncio.run(go())
    assert folder_ids.isdisjoint(view_ids), (
        f"{spec.label}: view ids collide with folder ids: {folder_ids & view_ids}"
    )
```

- [ ] **Step 7: Run the whole contract suite**

Run: `pytest tests/test_connector_contract.py -v`
Expected: PASS for all three connectors, including `test_every_shipped_connector_is_registered` and the new collision test. This must **pass**, not skip — a skip on the registration test means the spec was not added.

- [ ] **Step 8: Run the full suite**

Run: `pytest tests/ -q`
Expected: PASS (no regressions).

- [ ] **Step 9: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/connectors/instapaper.py tests/test_instapaper.py tests/test_connector_contract.py
git commit -m "Add the Instapaper connector and register it in the contract"
```

---

### Task 7: Config, app wiring, and test isolation

**Files:**
- Modify: `src/later_ink/config.py` (add `get_instapaper_config`)
- Modify: `src/later_ink/main.py` (instantiate in `lifespan`)
- Modify: `tests/conftest.py` (extend `_CONNECTOR_ENV`)
- Modify: `.env.example` (Instapaper section)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `InstapaperConnector` (for wiring); env vars.
- Produces: `config.get_instapaper_config() -> dict[str, str] | None` — keys `consumer_key`, `consumer_secret`, `oauth_token`, `oauth_token_secret`, matching `InstapaperConnector.__init__` for `InstapaperConnector(**cfg)`.

- [ ] **Step 1: Write the failing config test**

Append to `tests/test_config.py` (uses the same style as the Wallabag config tests already there):

```python
from later_ink import config


def test_get_instapaper_config_returns_none_when_incomplete(monkeypatch):
    for var in (
        "INSTAPAPER_CONSUMER_KEY",
        "INSTAPAPER_CONSUMER_SECRET",
        "INSTAPAPER_OAUTH_TOKEN",
        "INSTAPAPER_OAUTH_TOKEN_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("INSTAPAPER_CONSUMER_KEY", "ck")
    assert config.get_instapaper_config() is None


def test_get_instapaper_config_trims_and_returns_dict(monkeypatch):
    monkeypatch.setenv("INSTAPAPER_CONSUMER_KEY", " ck ")
    monkeypatch.setenv("INSTAPAPER_CONSUMER_SECRET", "cs")
    monkeypatch.setenv("INSTAPAPER_OAUTH_TOKEN", "ot")
    monkeypatch.setenv("INSTAPAPER_OAUTH_TOKEN_SECRET", "ots")
    assert config.get_instapaper_config() == {
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "oauth_token": "ot",
        "oauth_token_secret": "ots",
    }


def test_get_instapaper_config_blank_value_is_missing(monkeypatch):
    monkeypatch.setenv("INSTAPAPER_CONSUMER_KEY", "ck")
    monkeypatch.setenv("INSTAPAPER_CONSUMER_SECRET", "cs")
    monkeypatch.setenv("INSTAPAPER_OAUTH_TOKEN", "ot")
    monkeypatch.setenv("INSTAPAPER_OAUTH_TOKEN_SECRET", "   ")
    assert config.get_instapaper_config() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_config.py -k instapaper -v`
Expected: FAIL with `AttributeError: module 'later_ink.config' has no attribute 'get_instapaper_config'`.

- [ ] **Step 3: Implement `get_instapaper_config`**

In `src/later_ink/config.py`, add beside `get_wallabag_config` (mirror its trim-and-all pattern exactly):

```python
def get_instapaper_config() -> dict[str, str] | None:
    keys = {
        "consumer_key": "INSTAPAPER_CONSUMER_KEY",
        "consumer_secret": "INSTAPAPER_CONSUMER_SECRET",
        "oauth_token": "INSTAPAPER_OAUTH_TOKEN",
        "oauth_token_secret": "INSTAPAPER_OAUTH_TOKEN_SECRET",
    }
    values = {k: os.environ.get(env, "").strip() for k, env in keys.items()}
    if not all(values.values()):
        return None
    return values
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_config.py -k instapaper -v`
Expected: PASS.

- [ ] **Step 5: Wire it into `main.py`**

In `src/later_ink/main.py`, find the self-host wiring in `lifespan`:

```python
    wallabag_cfg = config.get_wallabag_config()
    if wallabag_cfg:
        _connectors["wallabag"] = WallabagConnector(**wallabag_cfg)
```

Add immediately after it:

```python
    instapaper_cfg = config.get_instapaper_config()
    if instapaper_cfg:
        _connectors["instapaper"] = InstapaperConnector(**instapaper_cfg)
```

Add the import at the top of `main.py`, beside the other connector imports:

```python
from .connectors.instapaper import InstapaperConnector
```

- [ ] **Step 6: Extend test isolation (same commit as wiring)**

In `tests/conftest.py`, extend `_CONNECTOR_ENV` with the four Instapaper vars:

```python
_CONNECTOR_ENV = (
    "READWISE_TOKEN",
    "WALLABAG_URL",
    "WALLABAG_CLIENT_ID",
    "WALLABAG_CLIENT_SECRET",
    "WALLABAG_USERNAME",
    "WALLABAG_PASSWORD",
    "INSTAPAPER_CONSUMER_KEY",
    "INSTAPAPER_CONSUMER_SECRET",
    "INSTAPAPER_OAUTH_TOKEN",
    "INSTAPAPER_OAUTH_TOKEN_SECRET",
)
```

- [ ] **Step 7: Update `.env.example`**

In `.env.example`, add after the Wallabag block:

```
#INSTAPAPER_CONSUMER_KEY=your_instapaper_consumer_key
#INSTAPAPER_CONSUMER_SECRET=your_instapaper_consumer_secret
# Mint the token pair once with: python -m later_ink.instapaper_auth
#INSTAPAPER_OAUTH_TOKEN=your_minted_oauth_token
#INSTAPAPER_OAUTH_TOKEN_SECRET=your_minted_oauth_token_secret
```

- [ ] **Step 8: Run the full suite**

Run: `pytest tests/ -q`
Expected: PASS. App-startup tests remain hermetic even if `INSTAPAPER_*` is set in the shell.

- [ ] **Step 9: Lint and commit**

```bash
ruff check src tests
git add src/later_ink/config.py src/later_ink/main.py tests/conftest.py tests/test_config.py .env.example
git commit -m "Wire the Instapaper connector into config and app startup"
```

---

### Task 8: The one-time token mint helper + README

**Files:**
- Create: `src/later_ink/instapaper_auth.py` (`mint_tokens` + `main` CLI)
- Create: `tests/test_instapaper_auth.py`
- Modify: `README.md` (Instapaper setup section)

**Interfaces:**
- Consumes: `oauthlib.oauth1`, `httpx`, `UpstreamError` from `base`.
- Produces: `async mint_tokens(consumer_key, consumer_secret, username, password, client: httpx.AsyncClient | None = None) -> tuple[str, str]` — returns `(oauth_token, oauth_token_secret)`; raises `UpstreamError` on rejection or an unexpected response.

- [ ] **Step 1: Write the failing test**

Create `tests/test_instapaper_auth.py`:

```python
import asyncio

import httpx
import pytest

from later_ink.connectors.base import UpstreamError
from later_ink.instapaper_auth import mint_tokens


def test_mint_tokens_parses_url_encoded_pair():
    def handler(request):
        assert request.url.path.endswith("/oauth/access_token")
        assert request.headers["Authorization"].startswith("OAuth ")
        body = request.content.decode()
        assert "x_auth_mode=client_auth" in body
        return httpx.Response(200, text="oauth_token=TOK&oauth_token_secret=SEC")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            return await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    assert asyncio.run(go()) == ("TOK", "SEC")


def test_mint_tokens_rejects_bad_status():
    def handler(request):
        return httpx.Response(401, text="Invalid xAuth credentials")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    with pytest.raises(UpstreamError) as e:
        asyncio.run(go())
    assert e.value.status == 401


def test_mint_tokens_rejects_unexpected_body():
    def handler(request):
        return httpx.Response(200, text="nothing useful here")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    with pytest.raises(UpstreamError):
        asyncio.run(go())
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_instapaper_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'later_ink.instapaper_auth'`.

- [ ] **Step 3: Implement the helper**

Create `src/later_ink/instapaper_auth.py`:

```python
"""One-time Instapaper token minting via xAuth.

Run once, out of band, to exchange an Instapaper username/password for the
OAuth token pair the connector uses. The password is never stored by the app.

    python -m later_ink.instapaper_auth
"""

import asyncio
import getpass
import os
import urllib.parse

import httpx
import oauthlib.oauth1

from .connectors.base import UpstreamError

BASE_URL = "https://www.instapaper.com/api/1"


async def mint_tokens(
    consumer_key: str,
    consumer_secret: str,
    username: str,
    password: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Exchange username/password for (oauth_token, oauth_token_secret).

    xAuth: a signed POST to oauth/access_token with x_auth_* params. The
    response is url-encoded, not JSON.
    """
    params = {
        "x_auth_mode": "client_auth",
        "x_auth_username": username,
        "x_auth_password": password,
    }
    body = urllib.parse.urlencode(params)
    oauth = oauthlib.oauth1.Client(
        consumer_key,
        client_secret=consumer_secret,
        signature_method=oauthlib.oauth1.SIGNATURE_HMAC_SHA1,
        signature_type=oauthlib.oauth1.SIGNATURE_TYPE_AUTH_HEADER,
    )
    uri, headers, signed_body = oauth.sign(
        f"{BASE_URL}/oauth/access_token",
        http_method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(uri, content=signed_body, headers=headers)
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Instapaper: {type(e).__name__}") from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise UpstreamError(
            f"Instapaper rejected the credentials ({resp.status_code})", resp.status_code
        )
    parsed = urllib.parse.parse_qs(resp.text)
    try:
        return parsed["oauth_token"][0], parsed["oauth_token_secret"][0]
    except (KeyError, IndexError) as e:
        raise UpstreamError("Instapaper returned an unexpected auth response") from e


def main() -> None:
    consumer_key = os.environ.get("INSTAPAPER_CONSUMER_KEY") or input("Consumer key: ").strip()
    consumer_secret = (
        os.environ.get("INSTAPAPER_CONSUMER_SECRET") or getpass.getpass("Consumer secret: ")
    )
    username = input("Instapaper username (email): ").strip()
    password = getpass.getpass("Instapaper password: ")
    token, secret = asyncio.run(
        mint_tokens(consumer_key, consumer_secret, username, password)
    )
    print("\nAdd these to your environment:\n")
    print(f"INSTAPAPER_OAUTH_TOKEN={token}")
    print(f"INSTAPAPER_OAUTH_TOKEN_SECRET={secret}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_instapaper_auth.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Document in the README**

In `README.md`, add an Instapaper subsection alongside the existing connector docs, covering: (1) requesting Full API access from Instapaper (each self-hosted user needs their own consumer key/secret — later.ink cannot bundle one), (2) minting the token pair with `python -m later_ink.instapaper_auth`, (3) the four `INSTAPAPER_*` env vars, and (4) the 500-most-recent-per-folder ceiling, with the folder-split workaround. Match the surrounding README's heading style and depth. Concretely, add:

```markdown
### Instapaper

Instapaper's Full API needs an OAuth 1.0a consumer key and secret. Request API
access from Instapaper (see <https://www.instapaper.com/api>) — each self-hosted
deployment needs its own; later.ink cannot ship one.

Mint the per-account token pair once (your password is never stored):

    python -m later_ink.instapaper_auth

Then set all four variables:

    INSTAPAPER_CONSUMER_KEY=...
    INSTAPAPER_CONSUMER_SECRET=...
    INSTAPAPER_OAUTH_TOKEN=...          # from the mint step
    INSTAPAPER_OAUTH_TOKEN_SECRET=...   # from the mint step

Note: Instapaper's API returns at most the 500 most-recent bookmarks per folder
and has no pagination beyond that. To reach older items, move them into
additional Instapaper folders (each folder exposes its own most-recent 500).
```

- [ ] **Step 6: Run the full suite and lint**

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
git add src/later_ink/instapaper_auth.py tests/test_instapaper_auth.py README.md
git commit -m "Add the Instapaper token mint helper and setup docs"
```

---

## Final verification (after all tasks)

- [ ] `pytest tests/ -q` — full suite green.
- [ ] `ruff check src tests` — clean.
- [ ] `python -c "import oauthlib.oauth1"` inside a fresh install of `requirements.txt` (or the built image) — confirms the runtime lock carries `oauthlib`, not just `pyproject.toml`. This is the check most likely to be skipped; it must **pass**, because a green local suite (dev lock installed) would not catch a missing runtime dependency.
- [ ] `git grep -n "hasattr(c, \"close\")" src/` returns nothing — the guard removal (Task 2) actually landed.
- [ ] Contract suite shows three connectors parametrized (`readwise`, `wallabag`, `instapaper`) across every contract test.
