# A connector contract, before a third connector

**Date:** 2026-08-18
**Status:** Approved design, ready for an implementation plan
**Context:** Cleanup ahead of the Instapaper connector planned for 0.7.0

## Problem

Later.ink has two connectors, Readwise and Wallabag, behind a three-method
`Connector` interface. A third (Instapaper) is next. Nothing today checks that
a connector satisfies the interface's *unwritten* requirements, and the two
existing implementations have already drifted apart on one of them.

**The drift is not hypothetical.** Given an HTTP 200 carrying a proxy error page
rather than JSON — what a flaky network path actually produces — the two
connectors behave differently:

```text
readwise.list_articles: UNHANDLED JSONDecodeError -> 500 for the user
wallabag.list_articles: UpstreamError (readable) — Wallabag returned an unexpected response
```

`WallabagConnector._get` guards `resp.json()`; `ReadwiseConnector._get` does not.
The Readwise path raises `json.JSONDecodeError`, which is not `UpstreamError`,
so it escapes as a 500 instead of a readable message on the e-reader.

**One unwritten requirement now has 0.6.0 resting on it.** The determinism work
made `dcterms:modified` come from `Article.content_date`, and ebooklib formats it
with `strftime("%Y-%m-%dT%H:%M:%SZ")` — appending a literal `Z` without
converting. A connector returning a timezone-aware datetime therefore writes a
local wall-clock time labelled UTC, and byte-stability drifts if that offset ever
moves. Both current connectors go through `base.parse_dt`, which normalises to
naive UTC. **Nothing enforces that a third one will.** The symptom would be
reading progress silently failing to sync, on another device, days later.

Each connector is tested bespoke (`test_readwise.py` 95 lines, `test_wallabag.py`
265) with no shared floor, so a third connector means a third bespoke suite and
no check that any of them agree.

## Goals

- A single artifact that states what a connector must do, executable against
  every connector.
- Enforce the naive-UTC invariant that the determinism guarantee depends on.
- Remove the verbatim duplication both connectors carry, and fix the JSON
  divergence for all connectors at once.
- Raise Readwise's coverage to match its surface before a third connector makes
  the asymmetry permanent.

## Non-goals

- Unifying the request/retry loop behind hooks. See "Rejected alternatives".
- Anything Instapaper-specific. This is the ground it lands on, not the feature.
- Reducing `main.py` (856 lines, the largest file). It will grow with Instapaper,
  but by config plumbing and route registration rather than tangled logic.
- Changing the `Connector` ABC's three abstract methods.

---

## 1. Injectable HTTP client

Both connectors build their own `httpx.AsyncClient` in `__init__`, and tests
reach in and overwrite the private `conn._client` (`tests/test_wallabag.py:43`).
A conformance suite needs a uniform, supported way to inject a mock transport.

Each connector gains a trailing keyword parameter:

```python
client: httpx.AsyncClient | None = None
```

defaulting to the client it builds today. This follows the pattern already in
the codebase — `build_epub(..., image_client: httpx.AsyncClient | None = None)`
exists for exactly this reason — rather than inventing a new convention. All
existing call sites are unaffected; the private-attribute surgery in
`test_wallabag.py` is replaced.

Instapaper inherits the convention instead of choosing its own.

## 2. `tests/test_connector_contract.py`

One file that *is* the contract: a registry of connectors and a set of shared
assertions run against each. Adding a connector is a registration, not a new
test file.

```python
ConnectorSpec(
    name="readwise",
    build=lambda transport: ReadwiseConnector("tok", client=_client(transport)),
    folder_id="later",
    article_id="42",
    responses={...},   # per-scenario handlers, see below
)
```

**Why the registry carries per-connector handlers.** The same *situation* looks
different on each API: a missing article is an empty `results` list from
Readwise and a `404` from Wallabag. The scenarios are shared; the bytes that
produce them are not. Each spec supplies a handler for:

| Scenario | Meaning |
|---|---|
| `ok` | A normal, well-formed response |
| `missing` | The article is not there |
| `error_500` | The service is broken |
| `unauthorized` | Credentials rejected |
| `non_json` | HTTP 200 whose body is not JSON |

**Why parameterized rather than an abstract test base class.** A base class each
connector's test file subclasses spreads the contract across files, so "what
must a connector do?" becomes something you reassemble rather than read. It also
makes pytest's discovery of inherited tests a thing contributors have to reason
about. One file readable top-to-bottom as a document is worth more here.

### The assertions

- `list_folders()` returns `Folder`s, each with a non-empty `str` `id` and
  `title`.
- `list_articles(folder_id)` returns `(list[Article], str | None)`. Every
  `Article.id` is a `str`. **`Article.content_date` must equal the source
  timestamp converted to UTC** — the determinism guard. Checked on both
  `list_articles` and `get_article_html`, because the latter is where
  `dcterms:modified` actually comes from (`main.py`'s `_epub_response` calls
  `get_article_html`, not `list_articles`); a connector whose two payloads
  disagree would otherwise pass on the untested path.
- `get_article_html(article_id)` returns `(Article, str)` with non-empty HTML.
- `missing` raises `ArticleUnavailable`, **not** `UpstreamError`. The two are
  handled differently: `ArticleUnavailable` carries a user-facing explanation
  and a 404/422, while `UpstreamError` means the service itself failed. They are
  easy for a new connector to conflate.
- `error_500`, `unauthorized`, an unreachable host, and `non_json` all raise
  `UpstreamError`. Nothing else escapes.
- `name` is a non-empty `str` and unique across the registry. It is part of the
  EPUB cache key (`main.py`, `cache_key(BUILD_VERSION, user, c.name, id)`), so a
  missing or colliding name would cross-contaminate cached books between
  connectors.
- `close()` is awaitable and safe to call twice.

**This suite is expected to fail on its first run**, on Readwise's `non_json`
case. That is the point, and the fix lands in the same change.

## 3. Shared helpers in `base.py`

Both `_get` implementations duplicate three things verbatim. They move to
`base.py` and both connectors adopt them:

```python
def retry_after_seconds(resp, *, default: float = 2.0, cap: float = 15.0) -> float
def raise_for_upstream(resp, service: str) -> None   # 401 / 429 / 4xx / 5xx -> UpstreamError
def decode_json(resp, service: str) -> dict          # ValueError -> UpstreamError
```

`service` is the human-readable name used in the message ("Readwise is
rate-limiting this account…"), so the existing wording is preserved rather than
homogenised into something less useful.

`decode_json` is what closes the divergence in the Problem section, for every
connector including ones not yet written.

## 4. Readwise coverage

The contract file establishes the shared floor. `tests/test_readwise.py` grows
to cover the Readwise-specific surface that has no Wallabag analogue and is
currently untested:

- the reading-time view filters (`_reading_time_view`, short/long thresholds,
  the book exclusion)
- `_list_books` paging
- category filtering (`READWISE_CATEGORIES`)
- the 429-retry path, including `Retry-After` honouring and the cap

Readwise is the primary connector with the largest surface and the thinnest
suite; leaving that asymmetry in place while adding a third connector makes it
permanent.

## Error handling

No behavioural change is intended anywhere except the Readwise `non_json` path,
which currently raises and should not. `raise_for_upstream` must preserve each
connector's existing messages and status codes — the tests asserting them today
should keep passing unchanged, and any that need editing indicate an accidental
behaviour change rather than a needed adjustment.

## Testing

`pytest tests/ -q` must stay green apart from the intended first-run failure
described in §2. The contract suite runs against both connectors from day one.
`ruff check src tests` clean.

## Risks

- **A contract that only encodes current behaviour.** The assertions are drawn
  from what the interface *requires* (determinism, cache-key integrity, error
  taxonomy), not from what the two implementations happen to do. The `non_json`
  case is the test of this: it is in the contract precisely because one
  implementation gets it wrong.
- **Registry drift.** A connector added without a registry entry is silently
  unverified. Mitigated by `test_every_shipped_connector_is_registered`, which
  walks every module in `later_ink.connectors` and fails if a `Connector`
  subclass defined there has no matching `ConnectorSpec` — and by the
  uniqueness assertion on `name`, which fails loudly if two entries collide.
- **Over-fitting to two connectors.** Real, and the reason the request loop is
  explicitly out of scope (below).

## Rejected alternatives

| Option | Why not |
|---|---|
| Unify the whole request/retry loop behind hooks | The loops genuinely differ: Wallabag re-authenticates on 401 by clearing tokens for a fresh password grant; Readwise has no re-auth. Instapaper brings a third auth model (request signing rather than a bearer token). An abstraction designed around two cases before the third is visible tends to fit none of them. Revisit after Instapaper, when the real variation is known. |
| Abstract test base class per connector | Spreads the contract across files; "what must a connector do?" stops being readable in one place. |
| Keep patching `conn._client` in tests | Works, but it is private-attribute surgery that every future connector's tests would copy, and the codebase already has the injectable-client pattern. |
| A test-only factory classmethod on `Connector` | Adds test-only API to production classes for less benefit than an injectable client. |
| Leave Readwise coverage for later | It is the primary connector and the thinnest-tested; "later" is after a third connector has cemented the asymmetry. |

## Sequencing

The injectable client (§1) must land before the contract suite (§2) can build
connectors uniformly. The shared helpers (§3) should land before or with the
suite, since the suite's `non_json` assertion fails until `decode_json` is
adopted. Readwise coverage (§4) is independent and can land last.
