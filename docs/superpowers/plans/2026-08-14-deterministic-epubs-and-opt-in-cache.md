# Deterministic EPUBs and Opt-In Cache — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make two downloads of the same unchanged article produce byte-identical EPUBs, so KOReader reading-progress sync works across devices, and add an opt-in on-disk cache for the residual nondeterminism that determinism cannot reach.

**Architecture:** ZIP entry mtimes are pinned to the ZIP epoch and `dcterms:modified` is set from a real upstream date (`saved_at`), removing the two timestamps that vary per build. Because the wall-clock image-fetch budget makes image-heavy articles genuinely nondeterministic, an opt-in filesystem cache stores rendered EPUBs — but only renders that completed cleanly, so a bad-network download can never freeze a degraded EPUB permanently.

**Tech Stack:** Python 3.11+, FastAPI, ebooklib, lxml, httpx, pytest, ruff. Docker with an unprivileged runtime user.

**Design spec:** `docs/superpowers/specs/2026-08-14-deterministic-epubs-and-opt-in-cache-design.md`. Read it before starting — it contains the measurements that justify every decision here.

## Global Constraints

- Branch: `epub-determinism-and-opt-in-cache`. All work lands as **one PR**.
- Lint: `ruff check src tests` must pass. Selected rules are `["E4", "E7", "E9", "F", "I", "UP"]`. **Line length is not enforced** — match surrounding style rather than wrapping to 88. Rule `I` means imports must be sorted.
- Tests: `pytest tests/ -q` must pass. **215 tests exist before this work starts**; none may regress. Run the suite once before Task 1 to confirm that baseline on your checkout rather than trusting this number.
- Python floor is 3.11 (`target-version = "py311"`). Use `X | None`, not `Optional[X]`.
- Caching is **off by default**. A deployment that sets nothing must write nothing to disk.
- Never let the cache break a download. Every cache failure degrades to "serve the freshly built bytes."
- The codebase comments *why*, not *what*. Match that: explain non-obvious reasoning, skip narration of what the code plainly does.
- Do not run `git push` or open a PR. Stop when the last task is committed.

## Ordering Constraints

These are hard dependencies, not preferences:

- **Task 1 before Task 2.** Task 2 passes a `content_date` into `build_epub`; the parameter does not exist until Task 1.
- **Task 3 before Task 6.** Task 6's caching decision reads `BuildResult.clean`, which Task 3 introduces.
- **Task 4 and Task 5 before Task 6.** Task 6 wires together the cache class and the config readers.
- **Task 7 after Task 5.** The entrypoint reads `EPUB_CACHE_DIR`, whose meaning Task 5 defines.
- Task 8 (docs) last, so it documents what was actually built.

Tasks 1–2 alone deliver the confirmed fix. Keep them as separate commits so determinism stays independently revertable if the cache work is backed out.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `src/later_ink/cache.py` | EPUB cache: key derivation, disabled no-op cache, disk-backed cache with LRU eviction. Knows nothing about HTTP, connectors, or EPUB internals. |
| `tests/test_cache.py` | Unit tests for the above, using `tmp_path`. |

**Modified:**

| File | Change |
|---|---|
| `src/later_ink/epub.py` | `ZIP_EPOCH`, `BUILD_VERSION`, `_pin_zip_timestamps`, `content_date` parameter, `BuildResult`, degradation reporting in `_embed_images`. |
| `src/later_ink/connectors/base.py` | `Article.content_date` field. |
| `src/later_ink/connectors/readwise.py` | Map `saved_at` / `created_at` into `content_date`. |
| `src/later_ink/connectors/wallabag.py` | Map `created_at` into `content_date`. |
| `src/later_ink/config.py` | `get_epub_cache_dir`, `get_epub_cache_max_bytes`. |
| `src/later_ink/main.py` | Build the cache in `lifespan`; cache lookup/store in `_epub_response`. |
| `docker-entrypoint.sh` | Create and chown `EPUB_CACHE_DIR` before dropping privileges. |
| `scripts/verify-privilege-drop.sh` | Assert the cache directory is created and app-owned. |
| `tests/test_epub.py` | Determinism tests; update call sites for the new return type. |
| `tests/test_security.py` | Update call sites for the new return type. |
| `README.md`, `.env.example` | Document the new vars and the migration consequence. |

---

## Task 1: Pin the two timestamps in `epub.py`

This is the fix proper. After this task, two builds of the same article are byte-identical.

**Files:**
- Modify: `src/later_ink/epub.py`
- Test: `tests/test_epub.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ZIP_EPOCH: tuple[int, int, int, int, int, int]` = `(1980, 1, 1, 0, 0, 0)`
  - `BUILD_VERSION: int` = `1`
  - `_pin_zip_timestamps(data: bytes) -> bytes`
  - `build_epub(..., content_date: datetime | None = None) -> bytes` — new keyword-only-in-practice parameter, appended to the existing signature so all current callers keep working.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_epub.py`. Add `from datetime import datetime` and `from later_ink.epub import ZIP_EPOCH, _pin_zip_timestamps, build_epub` to the imports (keep them sorted for ruff `I`).

```python
def _zip_with_mtime(dt: tuple[int, int, int, int, int, int]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mt = zipfile.ZipInfo("mimetype", date_time=dt)
        mt.compress_type = zipfile.ZIP_STORED
        z.writestr(mt, b"application/epub+zip")
        z.writestr(zipfile.ZipInfo("EPUB/x.xhtml", date_time=dt), b"<html/>")
    return buf.getvalue()


def _partial_md5(data: bytes) -> str:
    """KOReader's kosync document hash (frontend/util.lua partialMD5).

    Twelve 1 KiB samples at exponentially increasing offsets. Binary matching
    is kosync's default, so this is the function that decides whether reading
    progress syncs between two devices.
    """
    h = hashlib.md5()
    for off in [0] + [(1024 << (2 * i)) & 0xFFFFFFFF for i in range(11)]:
        if off >= len(data):
            break
        h.update(data[off : off + 1024])
    return h.hexdigest()


def test_pin_zip_timestamps_normalizes_differing_mtimes():
    # Two archives identical but for their entry mtimes must normalize to the
    # same bytes. This is the test that fails loudly if the pinning is dropped;
    # comparing two live builds cannot do that job, because two builds inside
    # the same clock second are already identical and the assertion passes
    # against completely unpinned code.
    a = _zip_with_mtime((2026, 8, 14, 10, 0, 0))
    b = _zip_with_mtime((2026, 8, 14, 10, 0, 2))
    assert a != b
    assert _pin_zip_timestamps(a) == _pin_zip_timestamps(b)


def test_pin_zip_timestamps_keeps_mimetype_first_and_stored():
    pinned = _pin_zip_timestamps(_zip_with_mtime((2026, 8, 14, 10, 0, 0)))
    zf = zipfile.ZipFile(io.BytesIO(pinned))
    assert zf.namelist()[0] == "mimetype"
    assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_build_epub_pins_every_entry_mtime():
    data = asyncio.run(build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d1"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert [i.date_time for i in zf.infolist()] == [ZIP_EPOCH] * len(zf.infolist())


def test_build_epub_is_byte_identical_across_builds():
    def one():
        return asyncio.run(
            build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d2")
        )

    a, b = one(), one()
    assert a == b
    assert _partial_md5(a) == _partial_md5(b)


def test_dcterms_modified_uses_content_date():
    data = asyncio.run(
        build_epub(
            title="T",
            author="A",
            html_content="<p>x</p>",
            identifier="d3",
            content_date=datetime(2025, 3, 4, 5, 6, 7),
        )
    )
    opf = zipfile.ZipFile(io.BytesIO(data)).read("EPUB/content.opf").decode()
    # Two loose assertions rather than one exact element string: lxml's
    # attribute ordering and namespace prefixing are not worth pinning here.
    assert 'property="dcterms:modified"' in opf
    assert "2025-03-04T05:06:07Z" in opf


def test_dcterms_modified_falls_back_to_sentinel():
    data = asyncio.run(build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d4"))
    opf = zipfile.ZipFile(io.BytesIO(data)).read("EPUB/content.opf").decode()
    assert "1980-01-01T00:00:00Z" in opf
```

Add `import hashlib` to the test file's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_epub.py -q -k "pin_zip or byte_identical or dcterms or pins_every"`
Expected: FAIL — `ImportError: cannot import name 'ZIP_EPOCH'`.

- [ ] **Step 3: Implement**

In `src/later_ink/epub.py`, add `from datetime import datetime` to the imports and `zipfile` alongside `io`. Add near the other module constants:

```python
# Bumped whenever a change to this module alters the bytes it produces. It is
# part of the cache key (cache.py), so bumping it retires every cached EPUB —
# without it, cached and freshly built copies of the same article would
# disagree indefinitely after a rendering change.
BUILD_VERSION = 1

# Every ZIP entry is stamped with this instead of the build time. Two downloads
# of the same article must be byte-identical or KOReader's kosync treats them
# as different documents and reading progress does not sync; the entry mtimes
# sit inside the first block its hash samples. 1980-01-01 is the earliest a DOS
# timestamp can encode and the conventional choice for reproducible archives.
#
# Deliberately a constant rather than an upstream date: this is container
# plumbing carrying no semantic claim, so it stays independent of the
# connectors. dcterms:modified is the opposite case — see build_epub.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Used for dcterms:modified when upstream gives us no date at all. Unreachable
# with Readwise, which always has saved_at; it exists so there is no undefined
# branch.
UNKNOWN_DATE = datetime(1980, 1, 1)
```

Add the rewriting pass:

```python
def _pin_zip_timestamps(data: bytes) -> bytes:
    """Rewrite an archive with fixed entry timestamps.

    Post-processes the finished file rather than patching ebooklib's writer:
    this depends only on the ZIP format, not on a private API that a version
    bump could move.

    Entry order is preserved because OCF requires `mimetype` to be the first
    entry and stored uncompressed. create_system is pinned because zipfile
    derives it from the host platform (0 on Windows, 3 elsewhere), which would
    otherwise make a Mac and a Linux host disagree on bytes.
    """
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            pinned = zipfile.ZipInfo(info.filename, date_time=ZIP_EPOCH)
            pinned.compress_type = info.compress_type
            pinned.external_attr = info.external_attr
            pinned.create_system = 3
            dst.writestr(pinned, src.read(info.filename))
    return out.getvalue()
```

Add the parameter to `build_epub`'s signature, after `image_client`:

```python
    content_date: datetime | None = None,
```

Extend the docstring with a sentence:

```
    content_date is when the source content entered the user's library; it
    becomes dcterms:modified. A real date rather than a fabricated one, and a
    stable one — see the design spec for why upstream's updated_at is not it.
```

Replace the final two lines of the function:

```python
    buf = io.BytesIO()
    epub.write_epub(buf, book, {"mtime": content_date or UNKNOWN_DATE})
    return _pin_zip_timestamps(buf.getvalue())
```

`{"mtime": ...}` is an option ebooklib's `_write_opf_metadata` honours for `dcterms:modified`; without it the writer stamps `datetime.datetime.now()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_epub.py -q`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Verify the tests actually fail against unpinned code**

This matters more than usual: a determinism test that passes on broken code is worse than no test. Temporarily change the last line of `build_epub` to `return buf.getvalue()`, run `pytest tests/test_epub.py -q -k "pin or dcterms or identical"`, and confirm failures. Then restore the line.

Expected: `test_build_epub_pins_every_entry_mtime` FAILs, because reverting that line is exactly what stops the pinning from reaching the output.

`test_pin_zip_timestamps_normalizes_differing_mtimes` will still **pass**, and that is correct — it calls `_pin_zip_timestamps` directly against a synthetic archive, so a change to `build_epub`'s return path cannot affect it. Do not "fix" it to fail.

If `test_build_epub_pins_every_entry_mtime` still passes when the line is reverted, the test is wrong — fix it before continuing.

- [ ] **Step 6: Run the full suite and lint**

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS. All 215 pre-existing tests still pass, plus the new ones.

- [ ] **Step 7: Commit**

```bash
git add src/later_ink/epub.py tests/test_epub.py
git commit -m "Make EPUB output byte-identical across builds

Two downloads of the same article differed in exactly two places, both
timestamps from ebooklib: dcterms:modified and the ZIP entry mtimes. KOReader's
kosync defaults to binary document matching and samples the start of the file,
where the entry mtimes live, so every re-download read as a new document and
reading progress reset.

ZIP mtimes are pinned to the ZIP epoch. dcterms:modified now comes from a
content_date argument, defaulting to a sentinel until the connectors supply
one."
```

---

## Task 2: Plumb a real `content_date` from the connectors

**Files:**
- Modify: `src/later_ink/connectors/base.py`, `src/later_ink/connectors/readwise.py`, `src/later_ink/connectors/wallabag.py`, `src/later_ink/main.py:748-759`
- Test: `tests/test_readwise.py`, `tests/test_wallabag.py`

**Interfaces:**
- Consumes: `build_epub(..., content_date=...)` from Task 1.
- Produces: `Article.content_date: datetime | None`, populated by both connectors.

- [ ] **Step 1: Write the failing tests**

In `tests/test_readwise.py`:

```python
def test_article_content_date_prefers_saved_at():
    art = _article_from_doc(
        {"id": 1, "title": "T", "saved_at": "2025-03-04T05:06:07Z", "created_at": "2024-01-01T00:00:00Z"}
    )
    assert art.content_date == datetime(2025, 3, 4, 5, 6, 7)


def test_article_content_date_falls_back_to_created_at():
    art = _article_from_doc({"id": 1, "title": "T", "created_at": "2024-01-01T00:00:00Z"})
    assert art.content_date == datetime(2024, 1, 1, 0, 0, 0)


def test_article_content_date_is_none_without_dates():
    assert _article_from_doc({"id": 1, "title": "T"}).content_date is None


def test_article_content_date_normalized_to_utc():
    # ebooklib formats this value with strftime("%Y-%m-%dT%H:%M:%SZ") — it
    # appends a literal Z without converting, so a non-UTC offset would be
    # written as UTC while carrying local wall-clock time.
    art = _article_from_doc({"id": 1, "title": "T", "saved_at": "2025-03-04T05:06:07+02:00"})
    assert art.content_date == datetime(2025, 3, 4, 3, 6, 7)
    assert art.content_date.tzinfo is None
```

Import `datetime` and `_article_from_doc` at the top of that file, sorted.

In `tests/test_wallabag.py`, matching the module's existing import of `_article_from_entry`:

```python
def test_entry_content_date_from_created_at():
    art = _article_from_entry({"id": 1, "title": "T", "created_at": "2024-05-06T07:08:09+00:00"})
    assert art.content_date == datetime(2024, 5, 6, 7, 8, 9)
    assert art.content_date.tzinfo is None


def test_entry_content_date_is_none_without_created_at():
    assert _article_from_entry({"id": 1, "title": "T"}).content_date is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_readwise.py tests/test_wallabag.py -q -k content_date`
Expected: FAIL with `AttributeError: 'Article' object has no attribute 'content_date'`.

- [ ] **Step 3: Add the field**

In `src/later_ink/connectors/base.py`, inside `@dataclass class Article`, after `image_url`:

```python
    # When this content entered the user's library. Feeds the EPUB's
    # dcterms:modified, so it must be stable: a value that moves when an
    # article is archived or starred would rewrite the file and reset the
    # reader's progress. Distinct from `updated` above, which defaults to
    # now() and is not usable for that.
    content_date: datetime | None = None
```

- [ ] **Step 4: Map it in the Readwise connector**

In `src/later_ink/connectors/readwise.py`, add a parser above `_article_from_doc`:

```python
def _parse_dt(value: str | None) -> datetime | None:
    """Parse an upstream ISO timestamp as naive UTC, or None.

    Normalized to UTC with the tzinfo dropped because ebooklib writes
    dcterms:modified with strftime("%Y-%m-%dT%H:%M:%SZ") — it appends the Z
    without converting, so an aware non-UTC value would be labelled UTC while
    carrying local wall-clock time.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)
```

Add `from datetime import UTC, datetime` to that module's imports.

Then in `_article_from_doc`, after `image_url=doc.get("image_url"),`:

```python
        # saved_at, not updated_at: archiving or starring moves updated_at and
        # last_moved_at but leaves saved_at alone, and a date that moved on
        # those actions would reset reading progress every time.
        content_date=_parse_dt(doc.get("saved_at") or doc.get("created_at")),
```

`_article_from_doc` serves both the list endpoint and `get_article_html`, so this single change covers the download path.

- [ ] **Step 5: Map it in the Wallabag connector**

`src/later_ink/connectors/wallabag.py` already has a `_parse_dt`, but it does not normalize the timezone. Replace its body with the same normalization:

```python
def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)
```

Add `UTC` to that module's `datetime` import. In `_article_from_entry`, add to the `kwargs` dict after `"image_url": ...`:

```python
        # created_at rather than updated_at, for the same reason as Readwise's
        # saved_at: updated_at moves when the entry is archived or starred.
        "content_date": _parse_dt(entry.get("created_at")),
```

- [ ] **Step 6: Pass it through in `main.py`**

In `_epub_response`, add to the `build_epub(...)` call after `raw_cover=...`:

```python
        content_date=article.content_date,
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS. Note that changing Wallabag's `_parse_dt` also affects `Article.updated`; if an existing Wallabag test asserts an aware datetime, update that assertion to the naive UTC equivalent rather than reverting the normalization.

- [ ] **Step 8: Commit**

```bash
git add src/later_ink/connectors src/later_ink/main.py tests/test_readwise.py tests/test_wallabag.py
git commit -m "Set dcterms:modified from the date the article was saved

The EPUB needs a stable value there, but it should be a real one rather than a
constant stamped into standardised metadata. Readwise's saved_at and Wallabag's
created_at both mean 'when this entered the library' and neither moves when an
article is archived or starred — unlike updated_at, which does, and which would
therefore reset reading progress on an unrelated action.

Parsed to naive UTC because ebooklib appends a literal Z to whatever it is
given without converting."
```

---

## Task 3: Report degraded renders from `build_epub`

**Files:**
- Modify: `src/later_ink/epub.py`, `src/later_ink/main.py:750`
- Test: `tests/test_epub.py`, `tests/test_security.py`

**Interfaces:**
- Consumes: `build_epub` from Tasks 1–2.
- Produces: `BuildResult` with fields `data: bytes`, `fallback_used: bool`, `images_failed: bool`, `budget_exhausted: bool`, and property `clean: bool`. **`build_epub` now returns `BuildResult`, not `bytes`.**

Why: `build_epub` degrades silently — an unparseable document becomes a fallback page, and images are dropped when a fetch fails or the wall-clock budget expires. If the cache stored those, one bad-network download would serve the degraded copy to every device until eviction, turning a transient problem into a permanent one.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_epub.py`:

```python
def test_clean_render_reports_no_degradation():
    result = asyncio.run(build_epub(title="T", author="A", html_content="<p>x</p>", identifier="r1"))
    assert result.clean
    assert not result.fallback_used
    assert not result.images_failed
    assert not result.budget_exhausted


def test_failed_image_fetch_marks_render_unclean():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await build_epub(
                title="T",
                author=None,
                html_content='<img src="https://93.184.216.34/a.png">',
                identifier="r2",
                image_client=client,
            )

    result = asyncio.run(run())
    assert result.images_failed
    assert not result.clean


def test_image_count_cap_still_counts_as_clean():
    # Hitting MAX_IMAGES is a limit on content, not on the clock: the same
    # article stops at the same image on every run, so the output is stable
    # and safe to cache. Only the wall-clock budget and outright fetch
    # failures make a render unrepeatable.
    html = "".join(f'<img src="https://93.184.216.34/{i}.png">' for i in range(MAX_IMAGES + 5))

    async def run():
        async with _mock_client() as client:
            return await build_epub(
                title="T", author=None, html_content=html, identifier="r3", image_client=client
            )

    result = asyncio.run(run())
    assert result.clean
    assert len(_image_files(zipfile.ZipFile(io.BytesIO(result.data)))) == MAX_IMAGES
```

Add a helper beside `_chapter_files`:

```python
def _image_files(zf: zipfile.ZipFile) -> list[str]:
    return sorted(n for n in zf.namelist() if n.startswith("EPUB/images/"))
```

Import `MAX_IMAGES` from `later_ink.epub`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_epub.py -q -k "clean or unclean or count_cap"`
Expected: FAIL — `AttributeError: 'bytes' object has no attribute 'clean'`.

- [ ] **Step 3: Implement the result type and reporting**

In `src/later_ink/epub.py`, add `from dataclasses import dataclass` to the imports and define above `build_epub`:

```python
@dataclass
class BuildResult:
    """An EPUB plus whether producing it went cleanly.

    A degraded render is still served — a book missing four images beats a
    failed download — but it must not be cached, or one bad-network request
    freezes the worse version for every device until eviction.
    """

    data: bytes
    fallback_used: bool = False
    images_failed: bool = False
    budget_exhausted: bool = False

    @property
    def clean(self) -> bool:
        return not (self.fallback_used or self.images_failed or self.budget_exhausted)
```

Change `_embed_images` to return its drop reasons. Its signature becomes:

```python
async def _embed_images(
    doc, client: httpx.AsyncClient
) -> tuple[list[epub.EpubItem], bool, bool]:
    """Fetch remote <img> targets and rewrite them to in-book paths.

    Returns the items plus two flags: whether any fetch failed, and whether the
    phase budget ran out. Both make the result unrepeatable — the budget is
    wall-clock, so the same article embeds every image on a fast connection and
    half of them on a slow one. The count and byte caps below are deliberately
    not reported: they are limits on content, so they bite at the same image on
    every run and the output stays stable.
    """
```

Inside it, initialize `images_failed = False` and `budget_exhausted = False` beside `total_bytes`, then:

```python
        if remaining <= 0:
            logger.debug("image budget spent; leaving the rest of the images remote")
            budget_exhausted = True
            break
```

```python
        if got is None:
            images_failed = True
            continue
```

and return `items, images_failed, budget_exhausted` at the end.

> Note on `got is None`: `fetch_bytes` collapses network failure, a disallowed content type, and an oversized response into a single `None`, and this code cannot tell them apart without changing `fetch.py`. Treating all of them as a failure is deliberately conservative — it may leave a few articles uncached, but it can never cache a render that would differ on the next attempt. If that proves too coarse, the fix is for `fetch.py` to report a reason, not for this to guess.

In `build_epub`, replace the `image_items = await _embed_images(doc, client)` line with:

```python
            image_items, images_failed, budget_exhausted = await _embed_images(doc, client)
```

Initialize `images_failed = False`, `budget_exhausted = False`, and `fallback_used = False` beside the existing `use_orig = False`, and set `fallback_used = True` in the `except Exception` branch that emits the fallback page. Finally:

```python
    buf = io.BytesIO()
    epub.write_epub(buf, book, {"mtime": content_date or UNKNOWN_DATE})
    return BuildResult(
        data=_pin_zip_timestamps(buf.getvalue()),
        fallback_used=fallback_used,
        images_failed=images_failed,
        budget_exhausted=budget_exhausted,
    )
```

- [ ] **Step 4: Update every call site**

`build_epub` now returns `BuildResult`. Every existing caller consumes the result as bytes, so each needs `.data` appended to the awaited or `asyncio.run(...)` expression. In helpers shaped `async def run(): return await build_epub(...)`, put `.data` on the `return` expression inside the helper.

Enumerate them rather than trusting a list — Task 1 added tests to `test_epub.py`, so any line numbers recorded before that are stale:

```bash
grep -n "build_epub(" src/later_ink/main.py tests/test_epub.py tests/test_security.py
```

Expected: 18 pre-existing call sites (12 in `test_epub.py`, 5 in `test_security.py`, 1 in `main.py`) **plus** the ones Task 1 introduced.

- `src/later_ink/main.py` — assign to `result`, then `epub_bytes = result.data`. Task 6 also reads `result.clean`; for now the bytes are enough.
- `tests/test_security.py` — all 5 sites.
- `tests/test_epub.py` — all 12 pre-existing sites, **and these four added in Task 1**, which consume bytes and will break otherwise:
  - `test_build_epub_pins_every_entry_mtime`
  - `test_build_epub_is_byte_identical_across_builds` (inside its `one()` helper)
  - `test_dcterms_modified_uses_content_date`
  - `test_dcterms_modified_falls_back_to_sentinel`

Do not touch the three tests added in Step 1 of *this* task — they already expect a `BuildResult`.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS. A `'BuildResult' object has no attribute` or `is not subscriptable` error means a call site was missed.

- [ ] **Step 6: Commit**

```bash
git add src/later_ink/epub.py src/later_ink/main.py tests/test_epub.py tests/test_security.py
git commit -m "Report whether an EPUB render degraded

build_epub falls back to a stub page on unparseable HTML and silently drops
images when a fetch fails or the wall-clock budget expires. All three produce a
valid but worse EPUB, and the upcoming cache must not store them: one bad-wifi
download would otherwise serve the degraded copy to every device until
eviction.

The count and byte caps are deliberately not treated as degradation. They limit
content rather than time, so they bite at the same image on every run."
```

---

## Task 4: The cache module

**Files:**
- Create: `src/later_ink/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `cache_key(build_version: int, user: str, connector: str, article_id: str) -> str`
  - `EpubCache` — disabled base class, `get(key) -> bytes | None` returning `None`, `put(key, data) -> None` doing nothing
  - `DiskEpubCache(EpubCache)` — `__init__(directory: str, max_bytes: int)`
  - `build_cache(directory: str | None, max_bytes: int) -> EpubCache`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache.py`:

```python
import io
import zipfile

from later_ink.cache import DiskEpubCache, EpubCache, build_cache, cache_key


def _epub_bytes(payload: bytes = b"<html/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        z.writestr(mt, b"application/epub+zip")
        z.writestr("EPUB/x.xhtml", payload)
    return buf.getvalue()


def test_disabled_cache_is_the_default(tmp_path):
    cache = build_cache(None, 1024)
    assert isinstance(cache, EpubCache)
    assert not isinstance(cache, DiskEpubCache)
    cache.put("k", _epub_bytes())
    assert cache.get("k") is None
    assert list(tmp_path.iterdir()) == []


def test_zero_cap_disables_the_cache(tmp_path):
    assert not isinstance(build_cache(str(tmp_path), 0), DiskEpubCache)


def test_put_then_get_round_trips(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    data = _epub_bytes()
    cache.put("k", data)
    assert cache.get("k") == data


def test_miss_returns_none(tmp_path):
    assert DiskEpubCache(str(tmp_path), 1024 * 1024).get("nope") is None


def test_corrupt_entry_is_a_miss_and_is_removed(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    cache.put("k", _epub_bytes())
    (tmp_path / "k").write_bytes(b"not an epub at all")
    assert cache.get("k") is None
    assert not (tmp_path / "k").exists()


def test_eviction_drops_oldest_until_under_cap(tmp_path):
    import os

    big = _epub_bytes(b"x" * 4096)
    cache = DiskEpubCache(str(tmp_path), len(big) * 2)
    for name, age in (("old", 1000), ("mid", 2000), ("new", 3000)):
        cache.put(name, big)
        os.utime(tmp_path / name, (age, age))
    cache.put("newest", big)
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total <= len(big) * 2
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "newest").exists()


def test_cache_directory_is_private(tmp_path):
    target = tmp_path / "cache"
    DiskEpubCache(str(target), 1024)
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_key_changes_with_every_component():
    base = cache_key(1, "local", "readwise", "a1")
    assert base != cache_key(2, "local", "readwise", "a1")
    assert base != cache_key(1, "other", "readwise", "a1")
    assert base != cache_key(1, "local", "wallabag", "a1")
    assert base != cache_key(1, "local", "readwise", "a2")


def test_key_is_a_bare_hex_digest(tmp_path):
    # The article id comes from upstream, so it must never reach a filesystem
    # path. Hashing the whole composed key also keeps a tenant secret off disk.
    key = cache_key(1, "sec/../../etc", "readwise", "../../evil")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_write_failure_does_not_raise(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    tmp_path.chmod(0o500)
    try:
        cache.put("k", _epub_bytes())  # must not raise
    finally:
        tmp_path.chmod(0o700)
    assert cache.get("k") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'later_ink.cache'`.

- [ ] **Step 3: Implement**

Create `src/later_ink/cache.py`:

```python
import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# A stored entry is checked before it is served. The cache is an optimisation
# for stability, never a source of truth: because generation is deterministic,
# anything that looks wrong can be discarded and rebuilt for free.
_ZIP_MAGIC = b"PK\x03\x04"
_MIMETYPE_OFFSET = 30
_MIMETYPE = b"mimetype"


def cache_key(build_version: int, user: str, connector: str, article_id: str) -> str:
    """Filename for one cached EPUB.

    Hashed rather than composed into a path for three reasons: the article id
    is chosen upstream and must never reach the filesystem, tenants must not
    collide, and in multi-tenant mode `user` is the catalog secret, which
    should not sit on disk in plaintext.
    """
    raw = f"{build_version}:{user}:{connector}:{article_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _looks_like_epub(data: bytes) -> bool:
    return (
        data.startswith(_ZIP_MAGIC)
        and data[_MIMETYPE_OFFSET : _MIMETYPE_OFFSET + len(_MIMETYPE)] == _MIMETYPE
    )


class EpubCache:
    """The disabled cache: every deployment gets this unless it opts in."""

    def get(self, key: str) -> bytes | None:
        return None

    def put(self, key: str, data: bytes) -> None:
        return None


class DiskEpubCache(EpubCache):
    """EPUBs on disk, bounded by total size, evicted least-recently-used first.

    Failure is never fatal. A download that cannot be cached is still a
    download, so every filesystem error here degrades to a miss rather than
    propagating to the request.
    """

    def __init__(self, directory: str, max_bytes: int):
        self.dir = Path(directory)
        self.max_bytes = max_bytes
        self.dir.mkdir(parents=True, exist_ok=True)
        # 0700 explicitly rather than via mkdir's mode, which the umask masks
        # and which does nothing for a directory that already existed. This
        # holds the user's reading material — a stronger claim on the
        # filesystem than anything else the app stores.
        os.chmod(self.dir, 0o700)

    def get(self, key: str) -> bytes | None:
        path = self.dir / key
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not _looks_like_epub(data):
            logger.warning("discarding a cache entry that is not an EPUB: %s", key)
            self._unlink(path)
            return None
        try:
            # Touch for LRU ordering; mtime is the only recency record kept.
            os.utime(path)
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes) -> None:
        try:
            # Written to a temp file and moved into place, so a reader never
            # sees a partial EPUB and a crash mid-write leaves nothing corrupt.
            # Two devices racing on the same article both write, which is
            # harmless: generation is deterministic, so the bytes are identical.
            fd, tmp = tempfile.mkstemp(dir=self.dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, self.dir / key)
            except OSError:
                self._unlink(Path(tmp))
                raise
        except OSError:
            logger.warning("could not cache %s; serving without storing", key, exc_info=True)
            return
        self._evict()

    def _evict(self) -> None:
        try:
            entries = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.dir.iterdir()]
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            if self._unlink(path):
                total -= size

    def _unlink(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def build_cache(directory: str | None, max_bytes: int) -> EpubCache:
    """The configured cache, or the disabled one.

    Both an unset directory and a zero cap mean off, matching how the rate
    limits in config.py read 0 as "turn it off".
    """
    if not directory or max_bytes <= 0:
        return EpubCache()
    try:
        return DiskEpubCache(directory, max_bytes)
    except OSError:
        # An unusable cache directory is a misconfiguration, not a reason to
        # refuse to serve books.
        logger.warning("EPUB cache directory %s is unusable; caching is off", directory, exc_info=True)
        return EpubCache()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cache.py -q && ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/later_ink/cache.py tests/test_cache.py
git commit -m "Add a bounded on-disk EPUB cache

Files rather than a SQLite table: these are multi-megabyte blobs, and store.py
exists for users and rate limiting. A file per entry needs no migration and no
coupling to the multi-tenant database, and mtime gives LRU ordering for free.

Not wired to anything yet."
```

---

## Task 5: Config readers

**Files:**
- Modify: `src/later_ink/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.get_epub_cache_dir() -> str | None`, `config.get_epub_cache_max_bytes() -> int`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, following that file's existing monkeypatch style:

```python
def test_epub_cache_dir_unset_is_none(monkeypatch):
    monkeypatch.delenv("EPUB_CACHE_DIR", raising=False)
    assert config.get_epub_cache_dir() is None


def test_epub_cache_dir_blank_is_none(monkeypatch):
    monkeypatch.setenv("EPUB_CACHE_DIR", "   ")
    assert config.get_epub_cache_dir() is None


def test_epub_cache_dir_is_read(monkeypatch):
    monkeypatch.setenv("EPUB_CACHE_DIR", "/data/epub-cache")
    assert config.get_epub_cache_dir() == "/data/epub-cache"


def test_epub_cache_max_bytes_defaults(monkeypatch):
    monkeypatch.delenv("EPUB_CACHE_MAX_BYTES", raising=False)
    assert config.get_epub_cache_max_bytes() == 512 * 1024 * 1024


def test_epub_cache_max_bytes_rejects_garbage(monkeypatch):
    monkeypatch.setenv("EPUB_CACHE_MAX_BYTES", "lots")
    assert config.get_epub_cache_max_bytes() == 512 * 1024 * 1024
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_config.py -q -k epub_cache`
Expected: FAIL with `AttributeError: module 'later_ink.config' has no attribute 'get_epub_cache_dir'`.

- [ ] **Step 3: Implement**

Append to `src/later_ink/config.py`:

```python
def get_epub_cache_dir() -> str | None:
    """Where to cache generated EPUBs. Unset (default) = off.

    Off by default because the app otherwise stores nothing: it reads the
    queue live and holds no article content. Turning this on trades that for
    byte-stable downloads, which is what reading-progress sync needs on an
    article whose images are slow enough to fetch differently between runs.

    In Docker, put it under /data so it lands on the volume that already
    persists and the entrypoint can take ownership of it before dropping
    privileges.
    """
    return os.environ.get("EPUB_CACHE_DIR", "").strip() or None


def get_epub_cache_max_bytes() -> int:
    """Total cache size before least-recently-used entries are dropped.

    0 turns the cache off, matching the rate-limit settings above."""
    return _int_env("EPUB_CACHE_MAX_BYTES", 512 * 1024 * 1024)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_config.py -q && ruff check src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/later_ink/config.py tests/test_config.py
git commit -m "Add EPUB_CACHE_DIR and EPUB_CACHE_MAX_BYTES

Both off by default: an unset directory or a zero cap means no cache, so a
deployment that sets nothing still writes nothing."
```

---

## Task 6: Wire the cache into the download route

**Files:**
- Modify: `src/later_ink/main.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `BuildResult.clean` (Task 3), `build_cache` / `cache_key` (Task 4), the config readers (Task 5), `BUILD_VERSION` (Task 1).
- Produces: `app.state.epub_cache`; `_epub_response(c, article_id, *, cache, cache_user)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_app.py`'s existing `client` fixture does not set `EPUB_CACHE_DIR`, and `lifespan` reads config when `TestClient` is entered — so the cache tests need their own fixture that sets the env *before* the `with TestClient(...)` block. Add both to `tests/test_app.py`:

```python
@pytest.fixture()
def cache_client(tmp_path, monkeypatch):
    """A client whose app started with the EPUB cache enabled.

    Separate from `client` because lifespan reads the config on entry, so
    setting the variable inside a test that already has a running app is too
    late.
    """
    cache_dir = tmp_path / "epub-cache"
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EPUB_CACHE_DIR", str(cache_dir))
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        yield c, cache_dir


def _serve_article(monkeypatch, html: str = "<p>Body</p>") -> None:
    async def fake_get_article_html(self, article_id):
        return Article(id=article_id, title="Test Article", author="Ann"), html

    monkeypatch.setattr(ReadwiseConnector, "get_article_html", fake_get_article_html)
    main._connectors["readwise"] = ReadwiseConnector("good-token")


def test_download_is_byte_identical_across_requests(client, monkeypatch):
    _serve_article(monkeypatch)
    try:
        first = client.get("/opds/readwise/articles/42.epub")
        second = client.get("/opds/readwise/articles/42.epub")
        assert first.status_code == 200
        assert first.content == second.content
    finally:
        main._connectors.clear()


def test_clean_render_is_cached(cache_client, monkeypatch):
    c, cache_dir = cache_client
    _serve_article(monkeypatch)
    try:
        assert c.get("/opds/readwise/articles/42.epub").status_code == 200
        assert len(list(cache_dir.iterdir())) == 1
    finally:
        main._connectors.clear()


def test_degraded_render_is_not_cached(cache_client, monkeypatch):
    # An image that will not fetch makes the render unclean. Caching it would
    # serve the missing-image copy to every device until eviction, which is a
    # worse outcome than the varying bytes the cache exists to prevent.
    c, cache_dir = cache_client

    async def no_images(*args, **kwargs):
        return None

    # Patched at the source rather than driven over the network: _epub_response
    # builds its own httpx client, so there is no transport to inject here.
    monkeypatch.setattr("later_ink.epub.fetch_bytes", no_images)
    _serve_article(monkeypatch, html='<p>x</p><img src="https://93.184.216.34/a.png">')
    try:
        assert c.get("/opds/readwise/articles/42.epub").status_code == 200
        assert list(cache_dir.iterdir()) == []
    finally:
        main._connectors.clear()
```

`Article`, `ReadwiseConnector`, `Fernet`, `TestClient`, `main` and `pytest` are all already imported at the top of that file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_app.py -q -k "cached"`
Expected: FAIL — `test_clean_render_is_cached` finds nothing in `cache_dir`, because nothing writes to it yet.

Note that `test_download_is_byte_identical_across_requests` already **passes** at this point: Task 1 made that true without any cache. It is a regression guard, not a red test.

- [ ] **Step 3: Build the cache at startup**

In `src/later_ink/main.py`, add to the imports:

```python
from .cache import EpubCache, build_cache, cache_key
from .epub import BUILD_VERSION, build_epub
```

(the existing `from .epub import build_epub` line becomes the second of these).

In `lifespan`, beside the other `app.state` assignments:

```python
    app.state.epub_cache = build_cache(
        config.get_epub_cache_dir(), config.get_epub_cache_max_bytes()
    )
```

- [ ] **Step 4: Use it in `_epub_response`**

```python
async def _epub_response(
    c: Connector, article_id: str, *, cache: EpubCache, cache_user: str
) -> Response:
    # Upstream is still consulted on a cache hit: the response needs the title
    # for its filename, and the ArticleUnavailable paths (deleted upstream, a
    # podcast whose transcript has not loaded) must keep reporting accurately
    # rather than serving a copy of something no longer in the account. What
    # the cache saves is the build, and with it the byte variance the image
    # phase introduces — not the round trip.
    article, html_content = await c.get_article_html(article_id)
    key = cache_key(BUILD_VERSION, cache_user, c.name, article_id)
    epub_bytes = cache.get(key)
    if epub_bytes is None:
        result = await build_epub(
            title=article.title,
            author=article.author,
            html_content=html_content,
            source_url=article.url,
            identifier=article.id,
            language=article.language or "en",
            preserve_styles=(article.category == "epub"),
            image_url=article.image_url,
            raw_cover=(article.category == "epub"),
            content_date=article.content_date,
        )
        epub_bytes = result.data
        # Only a clean render is stored. A degraded one is served — a book
        # missing four images beats a failed download — but caching it would
        # make a transient network problem permanent.
        if result.clean:
            cache.put(key, epub_bytes)
    safe_title = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in article.title)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.epub"'},
    )
```

- [ ] **Step 5: Update both callers**

`opds_epub` (around line 562) has no `Request` today; add one so it can reach `app.state`, matching `tenant_epub`'s existing signature style:

```python
@app.get("/opds/{connector}/articles/{article_id}.epub")
async def opds_epub(connector: str, article_id: str, request: Request):
    ...
    # Single-tenant: one user, so the key needs only a constant to occupy the
    # slot the tenant secret fills in multi-tenant mode.
    return await _epub_response(
        c, article_id, cache=request.app.state.epub_cache, cache_user="local"
    )
```

`tenant_epub` (around line 629) already receives `request`:

```python
    return await _epub_response(
        c, article_id, cache=request.app.state.epub_cache, cache_user=secret
    )
```

- [ ] **Step 6: Run the full suite**

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/later_ink/main.py tests/test_app.py
git commit -m "Serve cached EPUBs when caching is enabled

Closes the gap determinism cannot: the image phase runs against a wall-clock
budget and skips failed fetches, so an image-heavy article on a slow connection
genuinely renders differently between runs. Storing the first clean render
fixes the bytes for every device.

Upstream is still consulted on a hit, so a deleted article or an unloaded
podcast transcript still reports accurately."
```

---

## Task 7: Create and own the cache directory in Docker

**Files:**
- Modify: `docker-entrypoint.sh`, `scripts/verify-privilege-drop.sh`

The app runs as uid 10001, and a volume-backed directory is root-owned on first boot. The entrypoint already creates and chowns the database directory as root before dropping privileges; the cache directory needs the same treatment or the app cannot write it. This is the failure mode the README documents at length for the database.

- [ ] **Step 1: Extend the entrypoint**

In `docker-entrypoint.sh`, inside the `if [ "$(id -u)" = "0" ]; then` block, after the database `chown` loop and **before** the `exec setpriv` line:

```sh
    # The EPUB cache, when configured. Same problem as DATA_DIR: a fresh
    # volume arrives root-owned and the app cannot create or write it once
    # unprivileged. Unset means no cache and nothing to do.
    CACHE_DIR="${EPUB_CACHE_DIR:-}"
    if [ -n "$CACHE_DIR" ]; then
        mkdir -p -- "$CACHE_DIR"
        CACHE_DIR="$(cd -P -- "$CACHE_DIR" && pwd -P)"
        if [ "$CACHE_DIR" = "/" ]; then
            echo "docker-entrypoint: EPUB_CACHE_DIR must be a directory, not /" >&2
            exit 78
        fi
        # The directory only, not its contents. A recursive walk would scale
        # with the number of cached books, and it is not needed: the app owns
        # the directory, so it can replace a stale root-owned entry, and one it
        # cannot read is treated as a cache miss and rebuilt.
        chown -- "$APP_USER:$APP_USER" "$CACHE_DIR"
    fi
```

- [ ] **Step 2: Check the script parses and passes shellcheck if available**

Run: `sh -n docker-entrypoint.sh && (command -v shellcheck >/dev/null && shellcheck docker-entrypoint.sh || echo "shellcheck not installed, skipped")`
Expected: no syntax errors.

- [ ] **Step 3: Extend the privilege-drop verification**

In `scripts/verify-privilege-drop.sh`, add `-e EPUB_CACHE_DIR=/data/epub-cache` to the `docker run -d` invocation (line 52-53), so the run exercises the new branch:

```sh
docker run -d --name "$ctr" -p "$PORT":8000 \
    -v "$vol":/data -e DATABASE_PATH=/data/app.db \
    -e EPUB_CACHE_DIR=/data/epub-cache "$IMAGE" >/dev/null
```

Then add a check beside the existing `/data` ownership one, following the same style:

```sh
# The cache directory the entrypoint was asked to create, checked the same way
# as /data: the mount itself, from outside, since the app must own the
# directory and not merely be able to write a file into it.
check "/data/epub-cache is owned by $APP_UID" \
    bash -c "docker run --rm -v '$vol':/data alpine stat -c '%u:%g' /data/epub-cache \
        | grep -qx '$APP_UID:$APP_UID'"
```

- [ ] **Step 4: Run the verification — it must PASS, not skip**

Run: `docker build -t later-ink:test . && ./scripts/verify-privilege-drop.sh later-ink:test`
Expected: seven `ok:` lines and exit status 0.

**This step must actually run.** A skipped Docker verification here is a missing prerequisite, not a lowered bar — it is the only check covering the exact failure (unprivileged app, root-owned volume) that would otherwise surface as a crash loop after deploy and nowhere in the test suite. If Docker is unavailable in the environment, stop and report that rather than marking the task done.

- [ ] **Step 5: Commit**

```bash
git add docker-entrypoint.sh scripts/verify-privilege-drop.sh
git commit -m "Create and own EPUB_CACHE_DIR before dropping privileges

A volume-backed directory arrives root-owned, and the app runs as uid 10001, so
without this the cache silently fails every write on a fresh deploy. Same
reasoning as the database directory above it.

The directory only, not a recursive walk: the app owns it and can replace a
stale entry, and an unreadable one is treated as a miss and rebuilt."
```

---

## Task 8: Documentation and migration notes

**Files:**
- Modify: `README.md`, `.env.example`

- [ ] **Step 1: Add the env vars**

Append to `.env.example`, matching the commented-out style of the entries around it:

```
# Optional: cache generated EPUBs on disk. Off by default — Later.ink otherwise
# stores nothing, reading your queue live. Turn it on if you read the same
# article on more than one device: an article with slow-loading images can
# otherwise render slightly differently between downloads, and KOReader's
# progress sync matches documents by file contents. Put it on the volume in
# Docker (/data/epub-cache); the entrypoint takes ownership of it at startup.
#EPUB_CACHE_DIR=/data/epub-cache
# Total cache size before the least recently used entries are dropped.
# Defaults to 512 MiB; 0 turns the cache off.
#EPUB_CACHE_MAX_BYTES=536870912
```

- [ ] **Step 2: Qualify the storage claim in the README**

The line "Run it on a NAS, a Raspberry Pi, a VPS, or your laptop — it stores nothing, reading your queue live from Readwise." becomes:

```markdown
**Free and open source (MIT), and built to self-host** with your own Readwise
token. Run it on a NAS, a Raspberry Pi, a VPS, or your laptop — by default it
stores nothing, reading your queue live from Readwise. (An optional EPUB cache
can be turned on for cross-device reading-progress sync; see below.)
```

- [ ] **Step 3: Document the cache**

Add a section after the **Lists** table:

```markdown
### Reading the same article on two devices

Downloads are byte-identical for a given article, which is what
reading-progress sync needs: KOReader's sync plugin matches documents by
hashing the file, so two copies that differ in any way count as two different
books and your position does not carry across.

One case can still differ between downloads. Images are fetched while the EPUB
is built, under a time limit, and any that do not arrive in time are left out.
The same article can therefore come out slightly different on a slow connection
than on a fast one. If you read across devices and hit this, turn on the cache:

```bash
-e EPUB_CACHE_DIR=/data/epub-cache
```

The first complete render of each article is then stored and served to every
device afterwards. A render that lost images is never stored, so a download on
bad wifi cannot leave you with the worse copy permanently. The cache holds 512
MiB by default (`EPUB_CACHE_MAX_BYTES`), dropping the least recently read
articles first, and it is off unless you set the directory — the default
install still stores nothing.

It caches the conversion, not your queue: Readwise is still contacted on every
download, so an archived or deleted article behaves as it always did.

Two things worth knowing:

- Enabling it means your article text and images sit on that disk. Fine on your
  own hardware; worth a thought on a shared host.
- If a cached article is later re-parsed upstream, you keep the cached version
  until it is evicted. That staleness is the trade: it is what keeps the bytes
  stable.

If you would rather not cache anything, KOReader can match documents by
filename instead of contents (*Progress sync* → *Document matching method*).
Filenames here are stable, so that works too — but it is a per-device setting
that applies to your whole library.
```

- [ ] **Step 4: Add the upgrade note**

Add below the existing 0.3.x upgrade note in the quickstart:

```markdown
> **Upgrading from ≤0.5.x?** EPUB files are now byte-identical between
> downloads, which is what makes reading-progress sync work across devices.
> Getting there changed the bytes once, so the first re-download of an article
> you already have on a device registers as a new book and its reading position
> starts over. This happens once.
```

- [ ] **Step 5: Verify the docs match what was built**

Re-read the two files and confirm every claim is true of the code as merged: default cap is 512 MiB, `0` disables, the directory is the opt-in gate, degraded renders are not cached, upstream is still contacted on a hit. Fix any drift.

Run: `pytest tests/ -q && ruff check src tests`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example
git commit -m "Document the EPUB cache and the one-time hash change

Also qualifies the zero-storage claim, which is now true of the default rather
than of every configuration."
```

---

## Final verification

- [ ] `pytest tests/ -q` — all tests pass, including the 215 that predate this work.
- [ ] `ruff check src tests` — clean.
- [ ] `./scripts/verify-privilege-drop.sh later-ink:test` — seven `ok:` lines, exit 0. Must pass, not be skipped.
- [ ] `git log --oneline main..HEAD` shows eight commits, with determinism (Tasks 1–2) ahead of the cache work so it can be reverted independently.
- [ ] Manual smoke check, if a Readwise token is available: download the same article twice and confirm `shasum` matches.

Do not push or open a PR.
