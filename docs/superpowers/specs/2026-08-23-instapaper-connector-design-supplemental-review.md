# Supplemental review: Instapaper connector design

**Original spec:** `docs/superpowers/specs/2026-08-23-instapaper-connector-design.md`  
**Review date:** 2026-08-23  
**Scope:** Internal consistency of the Instapaper connector design and consistency with the current codebase on the `instapaper-connector` branch. This document is supplemental; the original spec was not edited.

## Summary

The design is broadly aligned with the connector contract introduced in #41: it preserves the three-method `Connector` interface, follows the injected-`httpx.AsyncClient` seam, treats Instapaper as self-host-only config like Wallabag, and accounts for the determinism requirement by sourcing `Article.content_date` from Instapaper's stable epoch field.

I found several plan-blocking issues to resolve before turning the spec into an implementation plan. The most important are dependency-lockfile handling, the contract `build` shape (it must return `(connector, client)` and give the injected client a `base_url`), and an internal mismatch in the described `bookmarks/get_text` error handling. Two items an earlier pass rated highly — the explicit `oauthlib.oauth1` import and the `requires_request_body` flag — are real but minor: each was verified against the runtime and is either caught at first import or already satisfied by the connector's form-encoded requests. Their severities are corrected below.

## Findings

### 1. New dependency must update the hashed lockfiles, not only `pyproject.toml`

**Severity:** High  
**Spec location:** §Global constants, §Testing  
**Codebase locations:** `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.github/workflows/ci.yml`, `Dockerfile`

The spec says to add `oauthlib>=3.2.2` to `pyproject.toml`. That is necessary but not sufficient in this codebase.

CI installs `requirements-dev.txt` with `--require-hashes`, then installs the project with `pip install -e . --no-deps --no-build-isolation`. The Docker runtime image installs `requirements.txt` with `--require-hashes`. Therefore adding only the `pyproject.toml` dependency leaves CI and the image without `oauthlib`; imports will fail even though the project metadata names the dependency.

**Recommended amendment:** The implementation plan should require regenerating at least:

- `requirements.txt`
- `requirements-dev.txt`

using the existing compile convention recorded at the top of those files. `requirements-build.txt` is unrelated unless the build backend dependency set changes.

### 2. The OAuth snippet needs an explicit `oauthlib.oauth1` import path

**Severity:** Low (verified: fails loudly and immediately, not a latent bug)  
**Spec location:** §2 OAuth 1.0a signing

The spec's snippet uses `oauthlib.oauth1.Client` and constants such as `oauthlib.oauth1.SIGNATURE_HMAC_SHA1`. Verified against oauthlib 3.3.1 in this environment: `import oauthlib` alone does not expose `oauthlib.oauth1` as an attribute (`module 'oauthlib' has no attribute 'oauth1'`). This is a snippet defect, not a design flaw — it surfaces on the first construction of `_OAuth1Auth`, so an implementer cannot miss it. The implementation must explicitly import the submodule, for example:

```python
import oauthlib.oauth1
```

or:

```python
from oauthlib import oauth1
```

Without that, the first construction of `_OAuth1Auth` raises `AttributeError: module 'oauthlib' has no attribute 'oauth1'`.

The spec is correct that `oauthlib.oauth1.Client` accepts `nonce=` and `timestamp=` constructor arguments, so deterministic signer tests are viable once the import is fixed.

### 3. The `httpx.Auth` signer should declare `requires_request_body` for robustness

**Severity:** Low (verified: current design works; the flag is defensive)  
**Spec location:** §2 OAuth 1.0a signing

The signer reads `request.content` so the form body is included in the OAuth signature base string. I verified the two relevant cases against httpx 0.28.1:

- **Form-encoded `data={...}` (what the connector uses):** `request.content` is populated inside `auth_flow` *without* the flag. The spec's approach works as written.
- **Streaming body (async generator `content=`):** accessing `request.content` without the flag raises `httpx.RequestNotRead`. The `requires_request_body` flag exists precisely for this case, and setting it makes httpx call `request.read()` before the auth flow.

So this is not a correctness bug for the connector as designed — every Instapaper call uses form-encoded `data=`. Setting the flag is a cheap guard against a future refactor to a streaming body, and it documents intent:

```python
class _OAuth1Auth(httpx.Auth):
    requires_request_body = True
```

**Recommended amendment:** Add `requires_request_body = True` to `_OAuth1Auth` as defensive hardening, and keep every connector request form-encoded (`data=...`). Not a blocker.

### 4. The contract test build needs a `base_url` **and** must return `(connector, client)`

**Severity:** High  
**Spec location:** §9 Contract  
**Codebase location:** `tests/test_connector_contract.py`

Two distinct problems here; the second was missed by the earlier pass.

**(a) `base_url` is required.** The spec says the Instapaper `ConnectorSpec.build` constructs the connector with an injected `httpx.AsyncClient(transport=MockTransport(handler))`. I verified against httpx 0.28.1 that this shape is not enough: with no `base_url`, `httpx.AsyncClient.post("bookmarks/list", ...)` raises `ValueError: unknown url type: '/bookmarks/list'` before the `MockTransport` handler is reached. Whether the path is written `bookmarks/list` or `/bookmarks/list` does not matter — both need a `base_url` to resolve.

**(b) `build` must return a `(connector, client)` tuple.** The current contract's `ConnectorSpec.build` signature is `Callable[[Callable], tuple[Connector, httpx.AsyncClient]]`, not `-> Connector`. The suite's `test_close_releases_the_http_client_and_is_safe_to_call_twice` unpacks `conn, client = spec.build(...)` and asserts `client.is_closed` after `close()`. The design spec (§9) and the older contract *plan* both still describe `build` as returning just the connector — that plan predates the tightening committed in `47ced3c` ("Make the close() contract test check the client actually closed"). An Instapaper `build` that returns a bare connector will break unpacking for the whole parametrized suite.

Both existing builders (`_build_readwise`, `_build_wallabag`) already return the tuple. Instapaper must match:

```python
def _build_instapaper(handler: Callable) -> tuple[Connector, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://instapaper.test/api/1",
        transport=httpx.MockTransport(handler),
    )
    connector = InstapaperConnector(
        consumer_key="ck",
        consumer_secret="cs",
        oauth_token="ot",
        oauth_token_secret="ots",
        client=client,
    )
    return connector, client
```

### 5. Error handling for `get_text` is internally inconsistent

**Severity:** High  
**Spec locations:** §6 Error handling, §9 Testing

The spec says Instapaper application errors are represented as array entries like:

```json
{"type": "error", "error_code": 1241, "message": "..."}
```

and that they can arrive under HTTP 200. It then defines `_raise_for_instapaper_error(items)` for JSON endpoints, but that helper only maps 1040 and 1042 specially and otherwise raises `UpstreamError(502)`.

Later, the table says several error codes must map differently for `get_text`:

- 1041 -> `ArticleUnavailable(422)`
- 1220 / 1221 -> `ArticleUnavailable(422)`
- 1241 -> `ArticleUnavailable(404)`
- 1550 -> `ArticleUnavailable(422)`

But the `get_text` path is described as parsing an error envelope only on HTTP `>= 400`, defaulting to `ArticleUnavailable(422)` if unknown or unparseable. That leaves an ambiguity: if Instapaper returns a HTTP-200 error envelope from `bookmarks/get_text`, should `get_article_html` parse it and raise `ArticleUnavailable`, or will it return the JSON error body as if it were article HTML?

The contract section compounds the ambiguity: it says the `missing` scenario's `get_text` returns an Instapaper error envelope, but does not specify whether the mocked status is 200 or a 4xx.

**Recommended amendment:** Split the error handling explicitly:

- JSON endpoints (`folders/list`, `bookmarks/list`): parse JSON arrays, detect error entries, and map to `UpstreamError` according to endpoint context.
- `bookmarks/get_text`: after status handling, detect both non-2xx error envelopes and HTTP-200 error envelopes before treating the body as HTML.

At minimum, the spec and tests should specify the mocked status code for the `missing` `get_text` envelope.

### 6. `get_article_html` cannot populate `Article.url` under the current composite-id design

**Severity:** Medium  
**Spec locations:** §3 Composite article id, §5 Connector methods  
**Codebase location:** `src/later_ink/epub.py`

The composite article id carries bookmark id, time, and title. That is enough for the contract's determinism assertions. However, `main._epub_response` passes `article.url` into `build_epub` as `source_url`, and `build_epub` emits it as `DC.source` metadata when present.

Readwise and Wallabag both populate `Article.url` on the download path. Instapaper's `get_text` endpoint has no metadata, so under the proposed design `get_article_html` has no way to populate `Article.url` unless the URL is also carried in the composite id.

This is not a connector-contract failure, but it is a behavior difference from the existing connectors and should be an explicit decision.

**Recommended amendment:** Either:

1. add `url` to the encoded article id, e.g. `{"i": ..., "t": ..., "n": ..., "u": ...}`, or
2. state explicitly that Instapaper EPUBs will not include source URL metadata because the API cannot fetch URL metadata by id.

If option 1 is chosen, the token remains path-safe because the whole JSON object is base64url-encoded.

### 7. Test environment isolation must include Instapaper env vars

**Severity:** Medium-high  
**Spec locations:** §1 Credentials, §9 Testing  
**Codebase location:** `tests/conftest.py`

`tests/conftest.py` removes connector-defining environment variables before every test so a developer's local `.env` does not register real self-host connectors during app startup. It currently removes only Readwise and Wallabag variables.

Adding Instapaper config without updating this fixture recreates the same class of hermeticity bug for anyone with `INSTAPAPER_*` variables in their shell or local env file.

**Recommended amendment:** Extend `_CONNECTOR_ENV` with:

- `INSTAPAPER_CONSUMER_KEY`
- `INSTAPAPER_CONSUMER_SECRET`
- `INSTAPAPER_OAUTH_TOKEN`
- `INSTAPAPER_OAUTH_TOKEN_SECRET`

### 8. `.env.example` should be updated alongside the README

**Severity:** Medium  
**Spec location:** §7 The one-time mint helper  
**Codebase location:** `.env.example`

The spec says the README gains an Instapaper setup section. The codebase also documents self-host connector variables in `.env.example`, and the README points users there for the full connector env-var list.

**Recommended amendment:** Add an Instapaper connector section to `.env.example`, including the four required variables and a note that tokens are minted once via the helper.

### 9. `config.get_instapaper_config()` should mirror Wallabag's trimming behavior

**Severity:** Low-medium  
**Spec location:** §1 Credentials, config, and wiring  
**Codebase location:** `src/later_ink/config.py`

The spec says `get_instapaper_config()` mirrors `get_wallabag_config()`. Wallabag trims values and treats blanks as missing:

```python
values = {k: os.environ.get(env, "").strip() for k, env in keys.items()}
if not all(values.values()):
    return None
```

**Recommended amendment:** The implementation plan should explicitly use the same `.strip()` pattern for all four Instapaper values.

### 10. The spec uses `_get` wording for POST-only Instapaper operations

**Severity:** Low  
**Spec locations:** §6 Error handling

The spec correctly states that all Instapaper API calls are POST. Later it refers to Instapaper's request helper as an `_get` variant, probably by analogy with the existing Readwise and Wallabag `_get` helpers.

**Recommended amendment:** Name the Instapaper helpers according to behavior, e.g. `_post_json(...)` and `_post_text(...)`, so the implementation plan does not accidentally inherit GET-oriented terminology.

### 11. JSON endpoint shape validation is underspecified (verified: produces a 500)

**Severity:** Medium (verified: leaks a 500, the exact failure class the contract's `decode_json` work removed)  
**Spec locations:** §5 Connector methods, §6 Error handling

`decode_json(resp, "Instapaper")` can return any JSON value — `base.decode_json` is deliberately typed `Any` for this reason. The spec's `_raise_for_instapaper_error(items: list)` and `list_articles` filter both assume a list, and `bookmarks/list` / `folders/list` are expected to return arrays. I verified what happens if a well-formed JSON *object* arrives instead (a proxy, or Instapaper wrapping a fault):

- `next((o for o in items if isinstance(o, dict) and o.get("type") == "error"), None)` iterates the dict's **keys** (strings), matches nothing, and returns `None` — so a real error envelope wrapped in an object is silently missed.
- The `list_articles` filter `[o for o in items if o.get("type") == "bookmark"]` then raises `AttributeError: 'str' object has no attribute 'get'`.

`AttributeError` is not `UpstreamError`, so it escapes to the reader as a 500 — precisely the "unexpected shape reaches the e-reader as a 500" failure the connector contract added `decode_json` to prevent (see the contract spec's Problem section). Leaving it unhandled would re-open that hole for the third connector.

**Recommended amendment:** After `decode_json`, validate that the top-level value is a list for Instapaper array endpoints and raise `UpstreamError("Instapaper returned an unexpected response")` otherwise, matching `decode_json`'s own wording.

### 12. The exact contract epoch should be spelled out in the implementation plan

**Severity:** Low  
**Spec location:** §3 Contract interaction, §9 Contract

The existing connector contract expects:

```python
CONTENT_DATE_UTC = datetime(2025, 1, 2, 1, 4, 5)
```

For the Instapaper encoded `article_id`, the corresponding Unix epoch is:

```text
1735779845
```

**Recommended amendment:** Use that epoch in the Instapaper `ConnectorSpec.article_id` and the `bookmarks/list` ok handler's `time` field so the list and download paths satisfy the same determinism assertion.

## Points that are consistent with the codebase

- The proposed constructor with a trailing `client: httpx.AsyncClient | None = None` matches the existing Readwise and Wallabag injection pattern.
- A shared `base.parse_epoch` belongs naturally next to `parse_dt`; `base.py` already imports `UTC` and `datetime`.
- Returning `next_cursor = None` for Instapaper is consistent with the connector contract, which permits `None` for no further pages.
- The base64url composite id is compatible with the current OPDS link generation. `opds.article_feed` inserts `article.id` directly into a URL path segment without quoting, and an unpadded base64url token uses only path-safe characters.
- The self-host-only wiring in `main.lifespan` is consistent with Wallabag and avoids touching the multi-tenant Readwise onboarding path.
- Adding a default no-op `Connector.close()` is compatible with existing test-only connectors, while the registered concrete connectors can still be required by contract tests to close their injected clients.
- The view/folder id-collision assertion matches `main._folder_response`, where a folder id wins over a view id if both exist.

## Suggested implementation-plan checklist additions

Ordered by priority. The first group would break the build or the contract suite if missed; the second is correctness/behaviour to decide before coding; the third is polish that fails loudly if forgotten.

**Blockers (build / suite will not go green):**

1. Make the Instapaper `ConnectorSpec.build` return a `(connector, client)` tuple and give the injected client a `base_url` (finding 4). Without the tuple the whole parametrized suite fails to unpack.
2. Add `oauthlib>=3.2.2` to `pyproject.toml` **and** regenerate `requirements.txt` and `requirements-dev.txt` with hashes, per the `uv pip compile` command recorded at the top of each lock (finding 1). Adding only `pyproject.toml` leaves CI and the image unable to import `oauthlib`.
3. Extend `tests/conftest.py`'s `_CONNECTOR_ENV` with the four `INSTAPAPER_*` vars (finding 7), or the suite is non-hermetic on any machine that has them set.

**Correctness / behaviour to settle in the plan:**

4. Specify and test `bookmarks/get_text` error-envelope behaviour for both HTTP 200 and HTTP `>= 400`, and fix the `_raise_for_instapaper_error` vs. table mismatch (finding 5). Pin the mocked status the `missing` `get_text` scenario uses.
5. After `decode_json`, guard that Instapaper array endpoints returned a list; raise `UpstreamError` otherwise (finding 11) — verified to otherwise 500.
6. Decide whether the composite id carries `url`; if not, document that Instapaper EPUBs omit `DC.source` (finding 6).
7. Use epoch `1735779845` for the Instapaper contract fixture's `time`/`article_id` so both determinism paths land on `CONTENT_DATE_UTC` (finding 12).
8. Mirror Wallabag's `.strip()`/`all(...)` trimming in `get_instapaper_config()` (finding 9).

**Polish (verified minor; fail loudly or are cosmetic):**

9. Import `oauthlib.oauth1` explicitly (finding 2) — caught at first construction.
10. Add `requires_request_body = True` to `_OAuth1Auth` as a defensive guard (finding 3) — not needed for the form-encoded requests as designed.
11. Add an Instapaper section to `.env.example` beside the README section the spec already calls for (finding 8).
12. Name the request helpers for POST behaviour, e.g. `_post_json` / `_post_text`, not `_get` (finding 10).

## Refinement notes (second-pass review)

This section records how the findings above were adjusted on review, so the next reader can trust the severities.

- **Corrected a miss in finding 4.** The first pass flagged only the missing `base_url`. The current `ConnectorSpec.build` contract returns a `(connector, client)` tuple and the close-contract test asserts `client.is_closed`, so a bare-connector `build` breaks the whole suite. This is the single most likely thing to be copied wrong from the design spec (§9) and the older contract plan, both of which predate commit `47ced3c` that tightened the test. Promoted to the top of the checklist.
- **Downgraded findings 2 and 3.** Verified against oauthlib 3.3.1 and httpx 0.28.1. The `oauthlib.oauth1` import fails at first construction (loud, unmissable), and `request.content` is already populated for the connector's form-encoded `data=` requests without `requires_request_body`. Both are worth doing but neither is a design blocker; keeping them at "High" would have misdirected implementation effort.
- **Made finding 11 concrete.** Confirmed a top-level JSON object (not array) makes the error helper silently miss the fault and the article filter raise `AttributeError` → 500 — the exact failure class the contract's `decode_json` was introduced to close. Raised from "Low-medium" to "Medium" with the mechanism shown.
- **Left findings 1, 5, 6, 7, 8, 9, 10, 12 as sound.** Spot-checked each against the codebase (lockfile install in `.github/workflows/ci.yml` and `Dockerfile`; `_raise_for_instapaper_error` vs. the §6 table; `_epub_response` → `build_epub` `source_url`; `_CONNECTOR_ENV` in `tests/conftest.py`; `get_wallabag_config` trimming; the `datetime(2025,1,2,1,4,5)` contract constant). No corrections needed.
- **Verified the "consistent with the codebase" list.** The contract's `assert cursor is None or isinstance(cursor, str)` confirms `next_cursor = None` is permitted, and `opds.article_feed` inserts `article.id` into the path unquoted — so the unpadded base64url token (path-safe alphabet, ASCII even for Unicode titles via `json.dumps` default `ensure_ascii=True`) is genuinely safe. These points stand.

**Overall:** the design is sound and consistent with the contract groundwork; none of the findings require changing the `Connector` ABC's three abstract methods or the determinism model. The blockers are all in the plan's mechanics (test wiring, lockfiles, error-path specification), not in the architecture.
