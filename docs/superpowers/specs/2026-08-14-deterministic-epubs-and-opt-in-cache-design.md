# Deterministic EPUBs and an opt-in EPUB cache

**Date:** 2026-08-14
**Status:** Approved design, ready for an implementation plan
**Branch:** `epub-determinism-and-opt-in-cache`

## Problem

Later.ink generates every EPUB on the fly. Two downloads of the *same* article
produce different bytes, which breaks cross-device reading-progress sync.

KOReader's `kosync` plugin matches documents by one of two methods, and
**binary is the default**:

```lua
checksum_method = CHECKSUM_METHOD.BINARY,
```

Binary matching uses `util.partialMD5`, which hashes twelve 1 KiB samples at
exponentially increasing offsets:

```lua
local step, size = 1024, 1024
for i = -1, 10 do
    file:seek("set", lshift(step, 2*i))
    local sample = file:read(size)
    ...
end
```

The first sample covers the very start of the file, where a ZIP's local file
headers live. Any change there is a different document, so progress does not
sync and reading position resets on the second device.

## Evidence

Measured against this codebase rather than assumed. Two builds of the same
image-free article, ~2 s apart:

```
identical: False
  EPUB/content.opf        content_same=False
  every other entry       content_same=True
  all 10 zip entries      mtime 10:12:56 vs 10:12:58
```

Only two things vary; both are timestamps, and both originate in ebooklib:

1. `<meta property="dcterms:modified">` in `content.opf`.
2. The ZIP entry mtimes, because `EpubWriter._write_items` calls
   `self.out.writestr(<str name>, ...)`, and `zipfile` stamps such entries with
   the current time.

Everything else — the generated cover JPEG, chapter XHTML, the identifier — is
already byte-stable. `_epub_response` passes `identifier=article.id`, and
`build_epub` derives a stable fallback from `sha256(f"{title}:{source_url}")`.

Confirmed the consequence directly, implementing KOReader's algorithm against
the two builds:

```
full sha256 equal: False
  partialMD5 [offset 0 (32-bit wrap)]  match=False
  partialMD5 [offset 256 (literal)  ]  match=False
  first differing byte offset: 295  (inside the first 1KB sample: True)
```

Byte 295 is a ZIP local-header mtime, inside KOReader's first sample. **The
timestamps alone are sufficient to break sync**; no image or content drift is
required.

A second measurement ruled out the cheaper fix of pinning only the ZIP and
leaving `dcterms:modified` at generation time:

```
SHORT article : bytes_identical=False kosync_match=False
   4149 differing bytes, first=295 ... any diff inside a sampled window: True
LONG article  : bytes_identical=False kosync_match=False
   342 differing bytes,  first=295 ... any diff inside a sampled window: True
```

Changing that timestamp string reflows the DEFLATE stream, so the damage is not
confined to the timestamp's own bytes. The OPF value must be stable too.

### The residual that determinism cannot fix

`_embed_images` runs against a wall-clock budget (`IMAGE_PHASE_BUDGET = 60.0`)
and silently skips any image whose fetch fails. Same article, same upstream
bytes, slower network → fewer embedded images → a genuinely different EPUB.
This is inherent to on-the-fly generation and is the reason a cache is worth
having. Upstream re-parsing an article is a second, rarer source.

## Goals

- Two downloads of the same unchanged article produce byte-identical EPUBs.
- Holds across devices, restarts, and hosts (Mac and Linux).
- No fabricated values in standardised bibliographic metadata.
- Caching is opt-in and off by default; the default deployment stores nothing.
- A degraded render never becomes permanent.

## Non-goals

- No TTL on cache entries.
- No `ETag`/`304` handling on the download route.
- No cache warming or pre-generation.
- No cross-user deduplication of identical articles.
- Not attempting byte-stability across Later.ink versions whose rendering
  changed; `BUILD_VERSION` handles that by invalidating instead.

---

## 1. Deterministic output (unconditional, no config)

Applies to every deployment whether or not caching is enabled.

**ZIP entry mtimes** are pinned to the ZIP epoch, `(1980, 1, 1, 0, 0, 0)` —
the earliest a DOS timestamp can encode, and what `SOURCE_DATE_EPOCH`-aware
build tools use. This is container plumbing carrying no semantic claim, and
deliberately independent of any connector data, so a change to date mapping
cannot perturb container bytes.

Add to `epub.py`:

```python
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

def _pin_zip_timestamps(data: bytes) -> bytes: ...
```

It is applied inside `build_epub` to the buffer `epub.write_epub` produces,
immediately before returning, and rewrites the archive entry by entry —
preserving **order**, `compress_type`, and `external_attr`, setting
`date_time=ZIP_EPOCH` and `create_system=3`.

- Order preservation is what keeps `mimetype` first and `ZIP_STORED`, as OCF
  requires.
- `create_system` is the one field `zipfile` varies by platform (`0` on
  Windows, `3` elsewhere). Pinning it makes output identical across hosts, not
  merely across runs on one machine.

Post-processing the finished archive is preferred over patching ebooklib's
internals: it depends only on the ZIP format, not on ebooklib's private
writer.

**`dcterms:modified`** is pinned via an option ebooklib already honours:

```python
if "mtime" in self.options:
    mtime = self.options["mtime"]
else:
    import datetime
    ...
```

so `epub.write_epub(buf, book, {"mtime": <datetime>})` sets it. The value is a
real upstream date — see §2.

**Normalise to UTC before passing it.** ebooklib formats the value with
`mtime.strftime("%Y-%m-%dT%H:%M:%SZ")`, which appends a literal `Z` without
converting. A timezone-aware non-UTC datetime would therefore be written as UTC
while carrying a local wall-clock time — wrong metadata, and a determinism
hazard if a connector's parsing ever yields a different offset for the same
instant. Convert to UTC and drop the tzinfo at the boundary. It is explicitly **not** the 1980 sentinel in the
normal case; stamping a fabricated date into standardised metadata was
considered and rejected.

`dcterms:modified` is kept rather than omitted: ebooklib emits
`version="3.0"` packages, and EPUB 3.0.1 requires exactly one. (EPUB 3.3
relaxed this to SHOULD, but that is not the version shipped.)

## 2. `content_date` plumbing

`dcterms:modified` needs a value that is real, meaningful, and stable across
actions that do not change content.

Add to `connectors/base.py`:

```python
@dataclass
class Article:
    ...
    content_date: datetime | None = None
```

Mapping:

| Connector | Source | Notes |
|---|---|---|
| Readwise | `saved_at`, falling back to `created_at` | Set in `_article_from_doc` |
| Wallabag | `created_at` via the existing `_parse_dt` | |

**Why `saved_at` and not `updated_at`.** Per the Reader API docs, `saved_at`
"marks the moment the document was stored in the user's Reader library", while
`last_moved_at` "indicates when the document's location was last changed (e.g.
moved from 'new' to 'archive')". Archiving moves `updated_at` and
`last_moved_at` but leaves `saved_at` alone. Keying the EPUB off `updated_at`
would rewrite the bytes — and reset reading progress — every time an article
was archived or starred.

**Why not `published_date`.** More bibliographically apt, but null for many
newsletters and tweets, able to move when Readwise re-parses a source, and
possibly pre-1980.

**Existing `Article.updated` is unusable.** It has
`field(default_factory=datetime.now)` and `_article_from_doc` never sets it, so
for Readwise it is literally `now()` per request. `content_date` is a new field
rather than a repurposing of it. (`Article.updated` continues to feed OPDS
`<updated>` and is left alone; that it is `now()` for Readwise is a
pre-existing oddity, out of scope here.)

**Single point of change.** `get_article_html` builds its `Article` through the
same `_article_from_doc`, so mapping the field once covers both the list and
detail paths.

**Fallback.** When `content_date` is `None`, `dcterms:modified` uses
`1980-01-01T00:00:00Z` as an explicit unknown-date sentinel. For Readwise this
is unreachable (`saved_at` is always present); it exists so the code has no
undefined branch.

## 3. Only clean renders are cached

`build_epub` degrades silently in three places: an unparseable document becomes
`_fallback_html`, `_embed_images` drops images, and `_fetch_cover` returns
nothing when the hero image will not fetch. Each returns a valid EPUB that is
worse than the one a good run produces.

The cover is easy to overlook — it is fetched before the document is even
parsed, and its failure looks exactly like "this article has no hero image" —
but it runs over the same network under the same timeout as the body images,
so the §3 test below catches it just the same. It is also the most visible
image in the book, and for an uploaded EPUB (`raw_cover`) a failed fetch
replaces the author's own designed cover with a generated one.

If the cache stored whatever the first request produced, **one bad-network
download would freeze the degraded version permanently** — the
missing-four-images copy served to every device until eviction. That converts a
transient problem into a permanent one, which is worse than the bug being
fixed.

`build_epub` therefore returns a result object instead of raw bytes:

```python
@dataclass
class BuildResult:
    data: bytes
    fallback_used: bool
    images_failed: bool
    budget_exhausted: bool

    @property
    def clean(self) -> bool:
        return not (self.fallback_used or self.images_failed or self.budget_exhausted)
```

`_embed_images` returns its drop reasons alongside its items, and `_fetch_cover`
returns whether it failed alongside the bytes — a bare `None` cannot carry that,
since it is also what "no hero image" looks like.

**Distinguish nondeterministic drops from deterministic caps.** Only these
make a render unclean:

- `fetch_bytes` returned `None` for a body image *or the cover* (network
  failure) → `images_failed`
- the wall-clock `IMAGE_PHASE_BUDGET` expired → `budget_exhausted`
- the HTML failed to parse → `fallback_used`

These do **not**, because they produce the same outcome on every run given the
same inputs, so caching them is safe:

- hitting `MAX_IMAGES`, `MAX_IMAGE_BYTES_TOTAL`, or `MAX_IMAGE_BYTES`
- an SVG rejected by `_sanitize_svg` as unparseable or non-SVG

The test to apply: **does the outcome depend on anything besides the input
bytes?** The caps are limits on *content* — an article with 45 images always
stops at the 30th, because images are fetched in document order and each has
the size it has. `IMAGE_PHASE_BUDGET` is a limit on *elapsed wall-clock time*,
so the same article embeds every image on a fast connection and half of them on
a slow one. Same inputs, different output — the exact failure this design
exists to remove.

Note also that the budget feeds back into individual fetches:

```python
timeout=min(IMAGE_FETCH_TIMEOUT, remaining),
```

so a shrinking budget shortens each remaining image's timeout and can turn a
would-be success into a failure. The clock does not merely truncate the loop.

Caveat on the caps: they are deterministic *given the same image bytes from
upstream*. If a CDN begins serving a differently-compressed variant, sizes
shift and a cap may bite at a different image. That is upstream drift — the
separate category the cache itself protects against once an entry exists.

A degraded render is served normally but not stored, so a later download can
produce the good one and cache that.

This changes `build_epub`'s signature; callers and `tests/test_epub.py` need
updating.

## 4. Cache module

New `cache.py`, deliberately not coupled to `store.py`:

```python
class EpubCache:
    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes) -> None: ...
```

with a disabled instance whose methods are no-ops, selected at startup from
config. `_epub_response` becomes get → build → put, and nothing else in the app
knows the cache exists.

**The cache does not skip the upstream fetch.** `get_article_html` still runs
on a hit: the response needs `article.title` for `Content-Disposition`, and the
`ArticleUnavailable` paths (article deleted upstream, podcast transcript not
yet loaded) must keep reporting accurately rather than serving a stale copy of
something no longer in the user's account. What the cache saves is the *build* —
HTML parsing, image fetching, cover generation — and, crucially, the byte
variance that comes with it. This is a stability feature, not a latency
feature, and the README should not describe it as a speed-up.

**Filesystem-backed, not a SQLite table.** `store.py` is about users and rate
limiting; EPUBs are multi-megabyte blobs. A file-per-entry cache needs no
schema migration and no coupling to the multi-tenant database. LRU ordering
comes from file mtime, touched on hit.

**Key:** `sha256(f"{BUILD_VERSION}:{user}:{connector}:{article_id}")`, hex, as
the filename. Three properties follow:

- The upstream-controlled `article_id` never reaches a filesystem path, so
  there is no traversal to defend against.
- Tenants cannot collide.
- `BUILD_VERSION` (a module-level constant in `epub.py`, bumped when rendering
  changes) invalidates every entry for free. Without it, cached and freshly
  built copies of the same article would disagree indefinitely after a
  rendering change.

**`user` component:** `"local"` in single-tenant mode; the tenant secret in
multi-tenant mode. Because the whole composed key is hashed, the secret never
lands on disk in plaintext. `_epub_response` gains a parameter for this;
`opds_epub` passes `"local"` and `tenant_epub` passes its secret.

## 5. Storage, eviction, concurrency

- **The cache directory belongs exclusively to the cache.** It creates the
  directory, narrows it to `0700`, and deletes from it. `EPUB_CACHE_DIR=/data`
  is a plausible thing for a self-hoster to type — §7 tells them to put the
  cache on the Docker volume, and `/data` *is* that volume, alongside `app.db`.
  So the invariant cannot rest on documentation: **eviction must only consider
  files the cache itself wrote**, recognised by name (a 64-char hex digest, or
  the temp-file prefix used for in-progress writes). Anything else is left
  alone and does not count against the cap. Pointing the cache at the directory
  holding `DATABASE_PATH` is additionally refused at startup.
- **Atomic writes.** Write to a temp file in the cache directory and
  `os.replace` into position, so a reader never sees a partial EPUB and a crash
  mid-write leaves no corrupt entry. The temp file carries a fixed prefix so
  that one orphaned by a crash between write and rename is still reclaimable by
  eviction rather than counting against the cap forever.
- **Concurrency.** Two devices requesting the same article simultaneously both
  build and both write. This is harmless precisely because of §1: they are
  writing identical bytes.
- **Eviction.** After a put, if total size exceeds `EPUB_CACHE_MAX_BYTES`,
  delete entries by oldest mtime until under the cap.
- **No TTL.** Indefinite stability is the feature being bought; expiry would
  reintroduce the churn being removed. Invalidation paths are a `BUILD_VERSION`
  bump and deleting the directory.

## 6. Config surface

Two functions in `config.py`, matching the file's existing shape:

```python
def get_epub_cache_dir() -> str | None:
    """Unset (default) = off, so nothing is written unless opted in."""

def get_epub_cache_max_bytes() -> int:
    """_int_env, default 512 MiB. 0 also disables."""
```

`EPUB_CACHE_DIR` unset is the opt-in gate. A cap of `0` disabling the cache
matches the "0 disables" convention already used by the rate-limit settings.

## 7. Docker integration

`docker-entrypoint.sh` currently starts as root, creates and chowns **only the
directory holding `DATABASE_PATH`**, then drops to uid 10001 via `setpriv`. A
cache directory anywhere else is root-owned on a fresh volume, and the app —
now unprivileged — cannot write it. This is the same class of failure the
README documents at length for the database.

Required:

- The entrypoint creates and chowns `EPUB_CACHE_DIR` when it is set, alongside
  what it already does for `DATABASE_PATH`.
- It must preserve the existing behaviour of skipping both steps when started
  as a non-root user.
- Documentation points `EPUB_CACHE_DIR` at a path under `/data` so it lands on
  the volume that already persists.

## 8. Error handling

The cache is never allowed to break a download.

- Any `OSError` on read is treated as a miss.
- Any failure on write or eviction is logged and swallowed; the response is
  served regardless.
- A stored entry is sanity-checked on read (ZIP magic, `mimetype` at the
  expected offset) and unlinked on failure rather than served. Because
  determinism guarantees we can rebuild, treating anything suspicious as a miss
  costs nothing.
- The directory is created `0o700`. It holds users' reading material, a
  stronger claim on the filesystem than anything the app stores today.
- `ArticleUnavailable` is raised before any build, so there is nothing to
  cache on that path.

## 9. Testing

**The determinism test has a trap.** Two builds inside the same second are
byte-identical *even without the fix*, so a naive test passes against broken
code. The test has to make the clock difference real rather than hope for one,
and must genuinely fail when the fix is reverted; verify that during
implementation.

*As built:* rather than monkeypatching the `time.time` that `zipfile` consults,
the test feeds `_pin_zip_timestamps` two synthetic archives that are identical
but for explicitly different `date_time` values, and asserts the pinned output
matches. Same trap avoided, with no clock manipulation and no dependence on how
`zipfile` reads the time — and verified to fail on revert. The whole-build test
alongside it asserts byte-identity across two live builds, which is the property
users experience but, on its own, would pass against unpinned code.

`tests/test_epub.py` gains:

- Timestamp pinning proven against a forced mtime difference (per above), plus
  byte-identity across two live builds.
- `mimetype` is the first entry and `ZIP_STORED`.
- All entry mtimes equal `ZIP_EPOCH`.
- `dcterms:modified` equals the `content_date` passed in, and the sentinel when
  it is `None`.
- A test implementing KOReader's `partialMD5` and asserting it matches across
  two builds — the property users actually experience.
- `BuildResult` flags: clean render, body-image fetch failure, **cover fetch
  failure** (both the generated and the `raw_cover` case), budget-exhausted
  render, fallback render, and that a `MAX_IMAGES` cap still counts as clean.
  Every flag needs a test that drives it *true*; an `assert not result.flag` in
  the clean case does not catch an assignment lost in a refactor. Empty
  `html_content` reaches the fallback branch, and
  `monkeypatch.setattr("later_ink.epub.IMAGE_PHASE_BUDGET", 0)` reaches the
  budget branch, so both are cheap.

New `tests/test_cache.py`:

- Off by default: no `EPUB_CACHE_DIR` means nothing is written.
- Hit and miss paths.
- A degraded render is served but not stored.
- Eviction drops the oldest entries and respects the cap.
- Eviction leaves a file the cache did not write, even when that means staying
  over the cap; an orphaned temp file is still reclaimed (§5).
- A corrupt entry is a miss and is unlinked.
- An `OSError` on write still serves the response.
- Changing `BUILD_VERSION` changes the key.

`tests/test_app.py`: the download route returns identical bytes on two calls,
with the cache both enabled and disabled — and, with it enabled, `build_epub`
is called *once* across the two requests. Counting builds is the only assertion
that observes the hit path; reading the entry back only proves the cache is not
write-only.

## 10. Documentation and migration

- **README:** the `EPUB_CACHE_*` vars; a qualifier on "it stores nothing";
  a note that enabling the cache puts article content on disk; the `/data`
  placement guidance from §7.
- **`.env.example`:** both new vars.
- **Release notes:** every EPUB's bytes change once, so anything already on a
  device reads as a new document to kosync and its reading position resets.
  Same shape as the 0.3.x identifier note already in the README.
- Worth mentioning in the README that KOReader's filename matching is an
  alternative for anyone who cannot upgrade: `Content-Disposition` uses
  `safe_title`, which is already stable.

## Risks

- **One-time progress reset** for every reader, as above. Unavoidable; any fix
  that changes bytes has this property. Mitigated by documenting it.
- **Stale content when caching is on.** By design. An article Readwise later
  re-parses keeps serving the cached render until eviction or a
  `BUILD_VERSION` bump. This is the trade being bought and belongs in the
  README.
- **ebooklib's `mtime` option is undocumented.** It is present in the vendored
  version and pinned in `requirements.txt`, but a future bump could remove it.
  The determinism tests would catch that; the implementation should not
  silently fall back.
- **Privacy posture shifts when the cache is enabled.** Relevant to the
  possible hosted instance, less so for self-hosting. Off by default keeps the
  default posture unchanged.

## Rejected alternatives

| Option | Why not |
|---|---|
| Pin `dcterms:modified` to 1980 | Fabricated value in standardised bibliographic metadata. |
| Pin only the ZIP, leave OPF at build time | Measured: still breaks kosync, because the OPF change reflows the DEFLATE stream. |
| Omit `dcterms:modified` | Required by EPUB 3.0.1, and ebooklib emits `version="3.0"`. |
| Derive the date from `Article.updated` | Literally `now()` for Readwise; moves on archive for Wallabag. |
| Cache the resolved image set instead of the EPUB | An EPUB here is mostly cover plus images, so it saves little, and it would have to cache fetch *failures* and budget outcomes to be deterministic. More machinery, weaker guarantee. |
| Cache everything including degraded renders | Freezes a bad-network render permanently. |
| SQLite-backed cache | Multi-megabyte blobs in a store built for users and rate limiting; needs a migration and couples the cache to the multi-tenant DB. |
| Tell users to switch kosync to filename matching | Works today, but it is a per-device setting affecting a user's whole library. Kept as a documented workaround, not the fix. |

## Sequencing note

**Decided: all of this lands as one PR on `epub-determinism-and-opt-in-cache`.**

§1 and §2 (determinism) are nonetheless a strict prefix — they stand alone and
deliver the confirmed fix with no storage, while §3–§8 (the cache) close the
image-timing residual. That ordering should still drive the commit sequence
within the PR, so determinism is reviewable on its own and a revert of the
cache work does not take the fix with it.

The `docker-entrypoint.sh` change in §7 is **in scope**. It is the part of this
most likely to break a deployment rather than a test, so it needs the
verification called out in §7 rather than test coverage alone.
