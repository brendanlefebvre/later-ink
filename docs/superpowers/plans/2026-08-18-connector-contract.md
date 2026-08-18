# Connector Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Later.ink an executable contract every connector must satisfy, before a third connector (Instapaper) is written against an unwritten one.

**Architecture:** Connectors gain an injectable `httpx.AsyncClient` so tests can wire a mock transport without private-attribute surgery. Three duplicated pieces of request handling move to `connectors/base.py`, which fixes a real divergence between the two implementations. A new parameterized test file states the contract and runs it against every connector; adding a connector is a registry entry. Readwise's bespoke tests grow to match its surface.

**Tech Stack:** Python 3.11+, httpx (with `MockTransport`), pytest, ruff.

**Design spec:** `docs/superpowers/specs/2026-08-18-connector-contract-design.md`. Read it first — it contains the measured evidence behind each decision.

## Global Constraints

- Branch: `connector-contract`. The spec is already committed there.
- Lint: `ruff check src tests` must pass. Rules are `["E4","E7","E9","F","I","UP"]`. **Line length is NOT enforced** — match surrounding style, do not wrap to 88. Rule `I` means imports must be sorted.
- Tests: `pytest tests/ -q` must pass. **278 tests exist before this work**; none may regress. Confirm that baseline yourself before Task 1 rather than trusting this number.
- Python floor 3.11 (`target-version = "py311"`): `X | None`, never `Optional[X]`.
- A bare `python` is NOT on PATH (pyenv shim without a global). Use `.venv/bin/python -m pytest tests/ -q` and `.venv/bin/python -m ruff check src tests`.
- `connectors/base.py` must stay free of app dependencies — it may not import `main`, `config`, `epub`, or `cache`.
- The codebase comments *why*, not *what*. Match that register; read around your changes first.
- Do not push or open a PR. Stop when the last task is committed.

## Ordering Constraints

- **Task 1 before Task 3.** The contract suite builds every connector through the injectable `client=` parameter, which Task 1 adds.
- **Task 2 before Task 3.** The contract asserts that a non-JSON response raises `UpstreamError`. Readwise fails that today; Task 2 fixes it. Running the suite first would mean committing a red test.
- **Task 4 is independent** and may land last.

A note on why Task 2 precedes Task 3, since the spec describes the suite as "expected to fail on its first run": that failure is demonstrated in **Task 2 Step 2**, as a focused RED test against the real defect. Ordering it that way keeps every commit on the branch green while still proving the defect existed. The contract suite in Task 3 then lands passing.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `tests/test_connector_contract.py` | The contract: a registry of connectors plus the assertions every one must satisfy. Readable top to bottom as a statement of what a connector is. |

**Modified:**

| File | Change |
|---|---|
| `src/later_ink/connectors/base.py` | Add `retry_after_seconds`, `raise_for_upstream`, `decode_json`. |
| `src/later_ink/connectors/readwise.py` | Injectable client; adopt the three helpers. |
| `src/later_ink/connectors/wallabag.py` | Injectable client; adopt the three helpers. |
| `tests/test_wallabag.py` | Use the injectable client instead of overwriting `conn._client`. |
| `tests/test_readwise.py` | Cover the reading-time views, book paging, category filtering, and the 429 retry. |

---

## Task 1: Injectable HTTP client

**Files:**
- Modify: `src/later_ink/connectors/readwise.py:116-123`, `src/later_ink/connectors/wallabag.py:75-93`
- Test: `tests/test_readwise.py`, `tests/test_wallabag.py:34-47`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ReadwiseConnector(token: str, categories: tuple[str, ...] = DEFAULT_CATEGORIES, client: httpx.AsyncClient | None = None)`
  - `WallabagConnector(url: str, client_id: str, client_secret: str, username: str, password: str, client: httpx.AsyncClient | None = None)`

Both default to the client they build today, so every existing call site is unaffected. This follows `build_epub(..., image_client: httpx.AsyncClient | None = None)`, which exists in `epub.py` for the same reason.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_readwise.py` (it currently has no httpx import — add `import httpx` in sorted position):

```python
def test_an_injected_client_is_used_instead_of_a_real_one():
    # The seam the contract suite needs: a connector must be constructable
    # against a mock transport without reaching into its privates.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "nextPageCursor": None})

    async def run():
        client = httpx.AsyncClient(
            base_url="https://readwise.test", transport=httpx.MockTransport(handler)
        )
        conn = ReadwiseConnector("tok", client=client)
        try:
            await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())
    assert seen and "readwise.test" in seen[0]
```

Add to `tests/test_wallabag.py`:

```python
def test_an_injected_client_is_used_instead_of_a_real_one():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600})
        return httpx.Response(200, json={"_embedded": {"items": []}, "page": 1, "pages": 1})

    async def run():
        client = httpx.AsyncClient(
            base_url="https://wb.example.com", transport=httpx.MockTransport(handler)
        )
        conn = WallabagConnector(
            url="https://wb.example.com",
            client_id="cid",
            client_secret="csec",
            username="user",
            password="pass",
            client=client,
        )
        try:
            await conn.list_articles("unread")
        finally:
            await conn.close()

    asyncio.run(run())
    assert "/api/entries.json" in seen
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_readwise.py tests/test_wallabag.py -q -k injected`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'client'`.

- [ ] **Step 3: Implement in the Readwise connector**

Replace `ReadwiseConnector.__init__`:

```python
    def __init__(
        self,
        token: str,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        client: httpx.AsyncClient | None = None,
    ):
        self._token = token
        self._categories = set(categories)
        # An injected client is taken as-is, including its auth: the caller that
        # supplies one is a test with a mock transport, and giving it the real
        # base URL would send those requests somewhere unintended.
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Token {token}"},
            timeout=30.0,
        )
```

- [ ] **Step 4: Implement in the Wallabag connector**

In `WallabagConnector.__init__`, add the parameter and use it:

```python
    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        client: httpx.AsyncClient | None = None,
    ):
```

and replace the client construction line with:

```python
        self._client = client or httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30.0)
```

Leave everything else in that constructor (`_creds`, `_access_token`, `_refresh_token`, `_expiry`, `_auth_lock`) exactly as it is.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_readwise.py tests/test_wallabag.py -q`
Expected: PASS.

- [ ] **Step 6: Retire the private-attribute surgery**

`tests/test_wallabag.py`'s `_make_conn` currently builds a connector and then overwrites `conn._client`. Replace its body so the client goes in through the constructor:

```python
def _make_conn(handler) -> WallabagConnector:
    return WallabagConnector(
        url="https://wb.example.com",
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pass",
        client=httpx.AsyncClient(
            base_url="https://wb.example.com",
            transport=httpx.MockTransport(handler),
        ),
    )
```

- [ ] **Step 7: Run the full suite and lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests`
Expected: PASS, 280 tests (278 + 2 new).

- [ ] **Step 8: Commit**

```bash
git add src/later_ink/connectors tests/test_readwise.py tests/test_wallabag.py
git commit -m "Let a connector be built around a supplied HTTP client

Tests reached into conn._client to swap in a mock transport, which every
future connector's tests would have copied. An optional client argument makes
the seam explicit and matches build_epub's image_client, which exists for the
same reason."
```

---

## Task 2: Shared request helpers, and the divergence they fix

**Files:**
- Modify: `src/later_ink/connectors/base.py`, `src/later_ink/connectors/readwise.py:125-149`, `src/later_ink/connectors/wallabag.py:146-179`
- Test: `tests/test_readwise.py`, `tests/test_wallabag.py`

**Interfaces:**
- Consumes: Task 1's `client=` parameter (the tests here use it).
- Produces, in `connectors/base.py`:
  - `retry_after_seconds(resp: httpx.Response, *, default: float = 2.0, cap: float = 15.0) -> float`
  - `raise_for_upstream(resp: httpx.Response, service: str) -> None`
  - `decode_json(resp: httpx.Response, service: str) -> dict`

**The defect this fixes.** Both connectors handle an HTTP 200 whose body is not JSON — what a proxy error page looks like. Wallabag raises a readable `UpstreamError`; Readwise raises an unhandled `json.JSONDecodeError`, which is not `UpstreamError` and so escapes as a 500 instead of a message on the e-reader.

**Deliberate deviation from spec §3.** The spec says to preserve each connector's existing wording. Two messages differ only cosmetically, and no test pins either (`tests/test_app.py:235` builds its own `UpstreamError` and asserts only the substring `"rate-limiting"`). Rather than parameterise the helper with per-connector strings, take the better wording of each as the shared one:

| Case | Readwise today | Wallabag today | Shared |
|---|---|---|---|
| 429 | "…is rate-limiting this account; try again in a minute" | "…is rate-limiting; try again in a minute" | "…is rate-limiting this account; try again in a minute" |
| 401 | "…rejected the stored token" | "…rejected the stored credentials" | "…rejected the stored credentials" |

So Wallabag's 429 message gains "this account" and Readwise's 401 says "credentials" rather than "token". Both are true of both. Call this out in the commit message so a reviewer reads it as intended.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_readwise.py`:

```python
def test_a_non_json_response_is_reported_as_an_upstream_error():
    # An HTTP 200 carrying a proxy error page rather than JSON. Left unguarded
    # this raises JSONDecodeError, which is not UpstreamError, so it escapes as
    # a 500 instead of a readable message on the e-reader.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            with pytest.raises(UpstreamError):
                await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())
```

Add `import pytest` and `from later_ink.connectors.base import UpstreamError` to that file, in sorted positions.

- [ ] **Step 2: Run it and confirm the real defect**

Run: `.venv/bin/python -m pytest tests/test_readwise.py -q -k non_json`
Expected: FAIL, and specifically with `json.JSONDecodeError` **not** being caught by `pytest.raises(UpstreamError)` — not with a missing-name error. If it fails for any other reason, stop and report: the test is not reaching the defect.

- [ ] **Step 3: Add the helpers to `base.py`**

Add near the other module-level helpers (after `parse_dt`). `base.py` does not currently import httpx — add `import httpx` in sorted position.

```python
def retry_after_seconds(resp: httpx.Response, *, default: float = 2.0, cap: float = 15.0) -> float:
    """How long to wait after a 429, from Retry-After, bounded.

    Bounded because an upstream is free to say "an hour" and an e-reader waiting
    on a download is not — past the cap, failing readably beats hanging.
    """
    try:
        return min(float(resp.headers.get("Retry-After", default)), cap)
    except ValueError:
        return default


def raise_for_upstream(resp: httpx.Response, service: str) -> None:
    """Turn an error status into an UpstreamError, or return for a good one.

    service names the upstream in the message, because the person reading it on
    an e-reader needs to know which account to go and fix.
    """
    if resp.status_code == 429:
        raise UpstreamError(f"{service} is rate-limiting this account; try again in a minute", 429)
    if resp.status_code == 401:
        raise UpstreamError(f"{service} rejected the stored credentials", 401)
    if resp.status_code >= 400:
        raise UpstreamError(f"{service} returned an error ({resp.status_code})", resp.status_code)


def decode_json(resp: httpx.Response, service: str) -> dict:
    """Parse a response body, or raise UpstreamError.

    A 200 is not a promise of JSON: a proxy or captive portal answers with an
    HTML error page and the status of its own choosing. Unguarded, that raises
    JSONDecodeError — not an UpstreamError — and reaches the reader as a 500.
    """
    try:
        return resp.json()
    except ValueError as e:
        raise UpstreamError(f"{service} returned an unexpected response") from e
```

- [ ] **Step 4: Adopt them in the Readwise connector**

Replace the tail of `ReadwiseConnector._get` (everything from `if resp.status_code == 429:` after the loop, to the end) with:

```python
        raise_for_upstream(resp, "Readwise")
        return decode_json(resp, "Readwise")
```

and replace the in-loop Retry-After block:

```python
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(retry_after_seconds(resp))
                continue
```

Add `decode_json`, `raise_for_upstream`, and `retry_after_seconds` to the existing `from .base import (...)` list, keeping it alphabetical.

- [ ] **Step 5: Adopt them in the Wallabag connector**

Replace the tail of `WallabagConnector._get` (from `if resp.status_code == 401:` after the loop, to the end) with:

```python
        raise_for_upstream(resp, "Wallabag")
        return decode_json(resp, "Wallabag")
```

and the in-loop Retry-After block:

```python
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(retry_after_seconds(resp))
                continue
```

Leave the 401 re-auth branch inside the loop exactly as it is — that is Wallabag-specific and is not being shared. Add the three names to its `from .base import (...)` list, alphabetically.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests`
Expected: PASS, 281 tests. If a Wallabag test fails on message text, check it against the table above — the two documented wording changes are intended; anything else is an accidental behaviour change and must be fixed rather than accommodated.

- [ ] **Step 7: Commit**

```bash
git add src/later_ink/connectors tests/test_readwise.py
git commit -m "Share the three pieces of request handling both connectors copied

Retry-After parsing, status-to-UpstreamError mapping, and JSON decoding were
duplicated, and had already drifted: Wallabag guarded resp.json() and Readwise
did not, so a 200 carrying a proxy error page reached the reader as a 500
instead of a readable message.

Two messages change wording, taking the better of each: Wallabag's 429 gains
'this account', and Readwise's 401 says 'credentials' rather than 'token'.
Both are true of both, and no test pinned either.

The request loops are deliberately not shared. Wallabag re-authenticates on a
401 and Readwise has no re-auth, and Instapaper will bring a third auth model;
an abstraction built around two of them would fit none."
```

---

## Task 3: The contract suite

**Files:**
- Create: `tests/test_connector_contract.py`

**Interfaces:**
- Consumes: Task 1's `client=` parameter on both connectors; Task 2's `UpstreamError` behaviour for non-JSON responses.
- Produces: a `SPECS` registry. A future connector is added by appending one `ConnectorSpec`.

- [ ] **Step 1: Write the file**

This task is the test — there is no separate implementation, so it goes green immediately if Tasks 1 and 2 are correct. That is the intended outcome; the defect was demonstrated in Task 2 Step 2.

```python
"""What every connector must do, run against every connector.

Read this file as the contract. Each connector registers how to build itself
and what its own API returns for a handful of situations; the assertions below
are shared, because the situations are shared even though the bytes are not —
a missing article is an empty results list on Readwise and a 404 on Wallabag.

Adding a connector means adding a ConnectorSpec, not another test file.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from later_ink.connectors.base import Article, ArticleUnavailable, Connector, Folder, UpstreamError
from later_ink.connectors.readwise import ReadwiseConnector
from later_ink.connectors.wallabag import WallabagConnector

FOLDER_ID_READWISE = "later"
FOLDER_ID_WALLABAG = "unread"
ARTICLE_ID = "42"


@dataclass
class ConnectorSpec:
    """Everything the contract needs to exercise one connector.

    `handlers` maps a scenario name to a transport handler, because the same
    situation looks different on each API. Keeping it on the spec — rather than
    in a lookup keyed by connector name — is what makes adding a connector a
    single registry entry with nothing else to remember.
    """

    label: str                                  # test id only
    cls: type[Connector]                        # for the class-level assertions
    build: Callable[[Callable], Connector]      # handler -> connector
    handlers: Callable[[str], Callable]         # scenario -> handler
    folder_id: str
    article_id: str


def _readwise_handler(scenario: str) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "error_500":
            return httpx.Response(500)
        if scenario == "unauthorized":
            return httpx.Response(401)
        if scenario == "non_json":
            return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
        if scenario == "missing":
            return httpx.Response(200, json={"results": [], "nextPageCursor": None})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": ARTICLE_ID,
                        "title": "An article",
                        "category": "article",
                        "saved_at": "2025-01-02T03:04:05+02:00",
                        "html_content": "<p>body</p>",
                    }
                ],
                "nextPageCursor": None,
            },
        )

    return handler


def _wallabag_handler(scenario: str) -> Callable:
    entry = {
        "id": int(ARTICLE_ID),
        "title": "An article",
        "url": "https://example.com/a",
        "created_at": "2025-01-02T03:04:05+02:00",
        "content": "<p>body</p>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(
                200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600}
            )
        if scenario == "error_500":
            return httpx.Response(500)
        if scenario == "unauthorized":
            # Wallabag re-authenticates once on a 401 before giving up, so this
            # must stay 401 on the retry too.
            return httpx.Response(401)
        if scenario == "non_json":
            return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
        if scenario == "missing":
            return httpx.Response(404)
        if request.url.path.startswith("/api/entries/"):
            return httpx.Response(200, json=entry)
        return httpx.Response(200, json={"_embedded": {"items": [entry]}, "page": 1, "pages": 1})

    return handler


SPECS = [
    ConnectorSpec(
        label="readwise",
        cls=ReadwiseConnector,
        build=lambda handler: ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        ),
        handlers=_readwise_handler,
        folder_id=FOLDER_ID_READWISE,
        article_id=ARTICLE_ID,
    ),
    ConnectorSpec(
        label="wallabag",
        cls=WallabagConnector,
        build=lambda handler: WallabagConnector(
            url="https://wb.test",
            client_id="cid",
            client_secret="csec",
            username="user",
            password="pass",
            client=httpx.AsyncClient(
                base_url="https://wb.test", transport=httpx.MockTransport(handler)
            ),
        ),
        handlers=_wallabag_handler,
        folder_id=FOLDER_ID_WALLABAG,
        article_id=ARTICLE_ID,
    ),
]


def _run(spec: ConnectorSpec, scenario: str, call):
    async def go():
        conn = spec.build(spec.handlers(scenario))
        try:
            return await call(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.fixture(params=SPECS, ids=lambda s: s.label)
def spec(request):
    return request.param


def test_name_is_a_non_empty_string(spec):
    # Connector.name is part of the EPUB cache key, so a blank or duplicated
    # name would cross-contaminate cached books between connectors.
    assert isinstance(spec.cls.name, str) and spec.cls.name


def test_connector_names_are_unique():
    # Asserted on the connectors themselves, not on the registry labels — two
    # connectors could be registered under different labels and still ship the
    # same `name`, which is the collision that matters.
    names = [s.cls.name for s in SPECS]
    assert len(names) == len(set(names))


def test_list_folders_returns_usable_folders(spec):
    folders = _run(spec, "ok", lambda c: c.list_folders())
    assert folders
    for f in folders:
        assert isinstance(f, Folder)
        assert isinstance(f.id, str) and f.id
        assert isinstance(f.title, str) and f.title


def test_list_articles_returns_articles_and_a_cursor(spec):
    articles, cursor = _run(spec, "ok", lambda c: c.list_articles(spec.folder_id))
    assert articles
    assert cursor is None or isinstance(cursor, str)
    for a in articles:
        assert isinstance(a, Article)
        assert isinstance(a.id, str) and a.id


def test_content_date_is_naive_utc(spec):
    # The determinism guard. dcterms:modified comes from content_date, and
    # ebooklib formats it with a literal Z and no conversion, so an aware value
    # would be written as UTC while carrying local wall-clock time — and the
    # bytes would drift if that offset ever moved. Both handlers above feed a
    # +02:00 timestamp precisely so a connector that forgets base.parse_dt
    # fails here.
    articles, _ = _run(spec, "ok", lambda c: c.list_articles(spec.folder_id))
    for a in articles:
        assert a.content_date is None or a.content_date.tzinfo is None


def test_get_article_html_returns_an_article_and_html(spec):
    article, html = _run(spec, "ok", lambda c: c.get_article_html(spec.article_id))
    assert isinstance(article, Article)
    assert isinstance(html, str) and html.strip()


def test_a_missing_article_raises_article_unavailable(spec):
    # Not UpstreamError: the two are handled differently. ArticleUnavailable
    # carries an explanation the reader sees and a 404/422; UpstreamError means
    # the service itself is broken.
    with pytest.raises(ArticleUnavailable):
        _run(spec, "missing", lambda c: c.get_article_html(spec.article_id))


@pytest.mark.parametrize("scenario", ["error_500", "unauthorized", "non_json"])
def test_upstream_problems_raise_upstream_error(spec, scenario):
    with pytest.raises(UpstreamError):
        _run(spec, scenario, lambda c: c.list_articles(spec.folder_id))


def test_an_unreachable_host_raises_upstream_error(spec):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async def go():
        conn = spec.build(handler)
        try:
            with pytest.raises(UpstreamError):
                await conn.list_articles(spec.folder_id)
        finally:
            await conn.close()

    asyncio.run(go())


def test_close_is_safe_to_call_twice(spec):
    async def go():
        conn = spec.build(spec.handlers("ok"))
        await conn.close()
        await conn.close()

    asyncio.run(go())
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_connector_contract.py -q -v`
Expected: PASS for both connectors on every test.

If `test_content_date_is_naive_utc` fails, a connector is not routing its date through `base.parse_dt` — fix the connector, not the test. If `test_upstream_problems_raise_upstream_error[non_json]` fails, Task 2 was not adopted correctly.

- [ ] **Step 3: Prove the contract has teeth**

A contract that cannot fail is decoration. Verify two of the assertions genuinely bite, then undo each change:

1. In `readwise.py`'s `_article_from_doc`, replace `parse_dt(...)` with `datetime.fromisoformat(doc.get("saved_at"))`. Run `pytest tests/test_connector_contract.py -q -k naive_utc`. Expected: FAIL for `readwise`. Restore.
2. In `base.py`, make `decode_json` return `resp.json()` without the try. Run `pytest tests/test_connector_contract.py -q -k non_json`. Expected: FAIL. Restore.

Record both outcomes in your report. If either passes while mutated, the assertion is not reaching the behaviour and must be fixed.

- [ ] **Step 4: Run the full suite and lint**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_connector_contract.py
git commit -m "State what a connector must do, and check every connector does it

Two connectors had already drifted on error handling with nothing to catch it,
and 0.6.0's byte-identical downloads now depend on every connector returning a
naive-UTC content_date — an unwritten rule a third connector could break, with
the symptom appearing days later on someone else's device.

The situations are shared even though the responses are not, so each connector
registers its own handlers and the assertions are common. Adding Instapaper
means adding a ConnectorSpec."
```

---

## Task 4: Readwise coverage

**Files:**
- Test: `tests/test_readwise.py`

**Interfaces:**
- Consumes: Task 1's `client=` parameter.
- Produces: nothing other tasks depend on.

Readwise is the primary connector, has the largest surface, and has the thinnest suite (95 lines against Wallabag's 265). These are the parts with no Wallabag analogue and no current coverage.

- [ ] **Step 1: Cover the reading-time views**

```python
def _article(words: int | None, category: str = "article") -> Article:
    return Article(id="1", title="T", word_count=words, category=category)


def test_short_reads_takes_articles_under_the_threshold():
    assert _reading_time_view(_article(minutes_to_words(SHORT_READ_MAX_MINUTES) - 1), short=True)
    assert not _reading_time_view(_article(minutes_to_words(SHORT_READ_MAX_MINUTES) + 1), short=True)


def test_long_reads_takes_articles_over_the_threshold():
    assert _reading_time_view(_article(minutes_to_words(LONG_READ_MIN_MINUTES) + 1), short=False)
    assert not _reading_time_view(_article(minutes_to_words(LONG_READ_MIN_MINUTES) - 1), short=False)


def test_an_unknown_length_is_not_a_short_read():
    # A missing word count means unknown length, not zero — it must not fall
    # into Short reads by default.
    assert not _reading_time_view(_article(None), short=True)
    assert not _reading_time_view(_article(0), short=True)


def test_books_are_excluded_from_long_reads():
    # Books have their own list, and would otherwise be most of Long reads.
    long_enough = minutes_to_words(LONG_READ_MIN_MINUTES) + 1
    assert not _reading_time_view(_article(long_enough, category=BOOK_CATEGORY), short=False)
```

Import `Article` and `minutes_to_words` from `later_ink.connectors.base`, and `BOOK_CATEGORY`, `LONG_READ_MIN_MINUTES`, `SHORT_READ_MAX_MINUTES`, `_reading_time_view` from `later_ink.connectors.readwise`.

- [ ] **Step 2: Cover book paging and category filtering**

```python
def test_books_are_listed_by_category_and_paginated():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [{"id": "9", "title": "A book", "category": "epub"}],
                "nextPageCursor": "page2",
            },
        )

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            return await conn._list_books(cursor="page1")
        finally:
            await conn.close()

    articles, cursor = asyncio.run(run())
    assert [a.id for a in articles] == ["9"]
    assert cursor == "page2"
    assert seen[0]["category"] == "epub"
    assert seen[0]["pageCursor"] == "page1"


def test_categories_outside_the_configured_set_are_dropped():
    articles, _ = _list(ReadwiseConnector("tok", categories=("article",)))
    assert [a.category for a in articles] == ["article"]
```

- [ ] **Step 3: Cover the 429 retry**

```python
def test_a_429_is_retried_once_and_honours_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(asyncio, "sleep", lambda d: _noop(slept, d))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"results": [], "nextPageCursor": None})

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            return await conn.list_articles("later")
        finally:
            await conn.close()

    asyncio.run(run())
    assert len(attempts) == 2       # retried once
    assert slept == [3.0]           # honoured Retry-After


def test_a_persistent_429_becomes_a_readable_error(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda d: _noop([], d))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    async def run():
        conn = ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        )
        try:
            with pytest.raises(UpstreamError) as caught:
                await conn.list_articles("later")
            assert caught.value.status == 429
        finally:
            await conn.close()

    asyncio.run(run())


async def _noop(record, delay):
    record.append(delay)
```

- [ ] **Step 4: Run and lint**

Run: `.venv/bin/python -m pytest tests/test_readwise.py -q -v && .venv/bin/python -m ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_readwise.py
git commit -m "Cover the Readwise surface that has no Wallabag counterpart

Readwise is the primary connector with the largest surface and the thinnest
suite. The reading-time views, book paging, category filtering, and the 429
retry had no tests; leaving that while adding a third connector would have
made the asymmetry permanent."
```

---

## Final verification

- [ ] `.venv/bin/python -m pytest tests/ -q` — passes, and the count has grown from 278.
- [ ] `.venv/bin/python -m ruff check src tests` — clean.
- [ ] `tests/test_connector_contract.py` passes for both connectors, and Task 3 Step 3's two mutations were confirmed to fail it.
- [ ] `grep -rn "_client" tests/` returns nothing — the private-attribute surgery is gone.
- [ ] `git log --oneline main..HEAD` shows the spec commit plus four task commits.

Do not push or open a PR.
