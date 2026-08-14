# [Later.ink](https://later.ink)

**Your Readwise Reader queue, on your e-reader.**

<p align="center">
  <img src="src/later_ink/assets/demo.gif" width="320"
       alt="Browsing the Later.Ink catalog in KOReader, downloading a saved article, and reading it as an EPUB with images.">
</p>

Later.Ink is a small server that turns your [Readwise Reader](https://readwise.io/read)
account into an [OPDS catalog](https://opds.io/). Point KOReader — or any ebook
reader that speaks OPDS, including on iPhone and iPad (Fablum, justRead,
PocketBook), Android (Moon+ Reader), or desktop (Thorium Reader) — at one URL
and browse your saved articles,
downloading any of them as a clean EPUB, generated on the fly from the article HTML
that Reader has already cleaned up.

No plugin, no sync daemon, and nothing to install from us — ebook readers already
speak OPDS.

**Free and open source (MIT), and built to self-host** with your own Readwise
token. Run it on a NAS, a Raspberry Pi, a VPS, or your laptop — it stores nothing,
reading your queue live from Readwise.

> **Scope:** later.ink is a *reading path, not a sync path*. It never writes
> back to Readwise, so articles stay in your queue as you read them. If you want
> finished articles archived and removed from the device automatically, the
> [Endle/readwisereader](https://github.com/Endle/readwisereader) KOReader
> plugin does that — this project trades write-back for zero install and working
> on any OPDS client. (Note: Readwise already serves Kindle natively via
> send-to-Kindle; the sweet spot here is Kobo, Boox, and other non-Kindle
> e-ink.)

## Self-host quickstart

> **Upgrading from ≤0.3.x?** The internal package was renamed for the brand:
> `read_later_opds` is now `later_ink`. Docker users just rebuild
> (`docker compose up -d --build --remove-orphans` — the flag retires the
> container from the old `opds` service name). If you run without Docker,
> reinstall (`pip install -e .`) and use the new module path:
> `python -m uvicorn later_ink.main:app ...`. Your `.env` and database are
> untouched. One cosmetic side effect: EPUB identifiers changed with the
> rename, so a re-downloaded article registers as a new book in your
> reader's library rather than an update to the old copy.

**Prebuilt image** (fastest — no checkout; amd64 & arm64, Pi included):

```bash
docker run -d --name later-ink -p 8000:8000 \
  -e READWISE_TOKEN=your_token_from_readwise.io/access_token \
  -e DATABASE_PATH=/data/app.db -v later-ink-data:/data \
  --restart unless-stopped \
  ghcr.io/brendanlefebvre/later-ink:latest
```

(Or pin a version tag, e.g. `:0.4.1`. Add `-e WALLABAG_*=...` vars
to serve Wallabag too — see [.env.example](.env.example) for the full list.
The same image drops straight into a Compose file or Portainer stack.)

**With Docker Compose, from source:**

```bash
git clone https://github.com/brendanlefebvre/later-ink.git
cd later-ink
mkdir -p ~/.config/later-ink && cp .env.example ~/.config/later-ink/env
# edit that file: READWISE_TOKEN=<your token from https://readwise.io/access_token>
docker compose up -d
```

**Without Docker** (Python 3.11+):

```bash
pip install later-ink
READWISE_TOKEN=your_token python -m uvicorn later_ink.main:app --host 0.0.0.0 --port 8000
```

Or from a source checkout:

```bash
git clone https://github.com/brendanlefebvre/later-ink.git
cd later-ink
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
mkdir -p ~/.config/later-ink && cp .env.example ~/.config/later-ink/env
# edit that file: READWISE_TOKEN=<your token from https://readwise.io/access_token>
python -m uvicorn later_ink.main:app --host 0.0.0.0 --port 8000
```

Config is read from `$XDG_CONFIG_HOME/later-ink/env` (default
`~/.config/later-ink/env`), falling back to `./.env`; variables already set in
the real environment always take precedence.

Your catalog is now at `http://your-host:8000/opds/`.

**KOReader setup:** top menu → magnifying glass → *OPDS catalog* → `+` → enter
the URL. Browse folders (Later, New, Shortlist, Archive, Feed), tap an article
to download and read. Images are embedded in the EPUB, so articles read fully
offline. Use KOReader's OPDS search box to find articles by title, author, or
summary; search scans your most recent saved items (a bounded slice of a very
large queue) rather than the entire history.

**Lists:** below the folders sit three cross-cutting lists, matching the
filtered views in Readwise Reader:

| List | What's in it |
| --- | --- |
| **Short reads** | Under 10 minutes — for when you have a bus ride, not an evening |
| **Long reads** | Over 20 minutes, books excluded |
| **Books** | EPUBs you've uploaded to Reader, wherever they've got to |

Short and long reads are drawn from Later, Shortlist, and New — what you meant
to read, not what you've already archived — and, like search, cover a bounded
slice of a very large queue rather than all of it. Books comes straight from
Readwise's own category filter, so it's complete and pages through everything.
The 10- and 20-minute thresholds come from Reader's documented view examples,
estimated at 250 words per minute.

**On iPhone/iPad:** use an ebook reader that supports OPDS — Fablum, justRead, or
PocketBook — and add the same catalog URL. **On desktop:** Thorium Reader works too.

**What appears:** articles, newsletters, PDFs, books (uploaded EPUBs), video
transcripts, tweet threads, and podcasts — each delivered as an EPUB, converted
from the content Readwise exposes. Multi-section books and long articles are split
into chapters with a navigable table of contents, and every EPUB opens on a
generated cover. A podcast converts only after you've loaded its transcript in
Readwise Reader (until then the API returns a stub, and the download reports
that). Configurable via `READWISE_CATEGORIES` (e.g. `article,pdf`).

## Hosted version

There's no public hosted instance running yet — self-hosting is the supported
path today. The server does include an optional multi-tenant mode (short,
e-ink-typeable catalog URLs like `later.ink/maple-crater-lantern-owl/`, with per-user
tokens encrypted at rest), so a free hosted instance may come later. If you want
to stand one up yourself, see the multi-tenant env vars in `.env.example`.

## Architecture

```
src/later_ink/
  main.py          # FastAPI routes: OPDS feeds, EPUB downloads, onboarding
  config.py        # env-var configuration
  opds.py          # OPDS 1.x Atom feed builder
  epub.py          # HTML → EPUB (ebooklib + lxml cleanup)
  covers.py        # generated EPUB covers (hero image + typographic fallback)
  store.py         # SQLite user store + word-based secret URLs
  words.py         # wordlist behind the secret URLs (e-ink-typeable words)
  ratelimit.py     # per-IP throttles: unknown-secret probes, signups, feeds
  pages.py         # server-rendered HTML pages
  payments.py      # Stripe verification (optional; inactive unless configured)
  connectors/
    base.py        # Connector interface: folders / views / articles / article HTML
    readwise.py    # Readwise Reader API v3 connector
    wallabag.py    # Wallabag API v2 connector (OAuth2)
```

Readwise and [Wallabag](https://wallabag.org/) are supported today (set the
`WALLABAG_*` vars in `.env.example` to enable Wallabag). More connectors
(Instapaper) are planned — the connector interface is three required methods.

## Development

```bash
pip install -e ".[dev]"
ALLOW_FREE_SIGNUP=1 python -m uvicorn later_ink.main:app --reload
pytest
```

`pyproject.toml` keeps loose `>=` ranges, which is what anyone installing
Later.ink from PyPI resolves against. CI and the Docker image instead install
from pinned, hashed lockfiles, so a release is built from the versions that
were actually tested. There are four, covering each layer:

| file | contents | used by |
|---|---|---|
| `requirements.txt` | runtime dependencies | the image, and the base for the others |
| `requirements-dev.txt` | runtime + `dev` extra | CI test job |
| `requirements-build.txt` | the PEP 517 backend (`hatchling`) | every step that builds the project |
| `.github/requirements-ci.txt` | workflow tooling (`build`, `pip-audit`) | CI and release jobs |

Regenerate after changing a dependency:

```bash
uv pip compile pyproject.toml --universal --generate-hashes \
  --python-version 3.11 -o requirements.txt
uv pip compile pyproject.toml --extra dev --universal --generate-hashes \
  --python-version 3.11 -o requirements-dev.txt
uv pip compile requirements-build.in --universal --generate-hashes \
  --python-version 3.11 -o requirements-build.txt
uv pip compile .github/requirements-ci.in --universal --generate-hashes \
  --python-version 3.11 -o .github/requirements-ci.txt
```

The last two exist because a PEP 517 build normally resolves its backend into a
throwaway environment, unpinned and unhashed — outside every other guarantee
here. Pinning it means passing `--no-build-isolation` (pip) or `--no-isolation`
(`build`) wherever the project is built, so the pinned backend is used instead
of a freshly fetched one. The Dockerfile builds a wheel in a first stage for the
same reason: without build isolation the backend would otherwise remain
installed in the shipped image.

### The base image pin

The lockfiles stop at the Python layer. Both `FROM` lines in the `Dockerfile`
are therefore pinned to the base image's **multi-architecture index digest**,
not the mutable `python:3.12-slim` tag — otherwise the interpreter and the OS
packages underneath every hash-pinned wheel are still whatever upstream last
published. The index digest rather than a single platform's manifest is what
keeps the `linux/amd64` + `linux/arm64` release build working.

Freezing OS packages freezes their CVEs too, so the pin is only safe alongside
something that refreshes it: Dependabot's `docker` ecosystem, weekly, which
rewrites the tag and the digest together.

The Python series is held at 3.12, by an `ignore` in `.github/dependabot.yml`
that drops minor and major version updates and lets digest-only refreshes
through. So the weekly pull requests carry rebuilds of the tag this project is
already on, and moving to a newer interpreter is a deliberate change rather than
one that arrives looking like a patch. The note beside that block says what to
change when the series does move.

To bump one by hand instead:

```bash
# The digest the tag currently points at. imagetools reads the registry
# without downloading layers.
docker buildx imagetools inspect python:3.12-slim | grep Digest
```

Nothing in CI compares a pinned digest against the tag written beside it, and
the distinction matters: such a check would measure *staleness*, not
correctness. When upstream rebuilds `python:3.12-slim` the tag moves to the new
image; the pinned digest stays pullable, it simply stops being what the tag
names. So a CI job comparing the two would go red on every correct pin the day
after a rebuild, and the only way to green it is to bump — which is Dependabot's
job, on its own schedule, rewriting tag and digest together. It would also put a
Docker Hub call on every CI run, against anonymous pull limits shared across
runner IPs.

The tag can therefore drift out of date, and it is worth checking when a pin
changes that the digest is a real image of the series the tag claims:

```bash
docker run --rm python:3.12-slim@sha256:<digest> python -V   # Python 3.12.x
docker run --rm python:3.12-slim@sha256:<digest> \
  sh -c 'grep ^PRETTY /etc/os-release'                       # Debian
```

### Running as an unprivileged user

`uvicorn` runs as `app`, uid/gid **10001**, not root. The image has no `USER`
instruction, though, and that is deliberate: `fly.toml` mounts a volume at
`/data` and Fly creates volumes root-owned, while `docker compose` bind-mounts
`./data`, where ownership comes from the host. Either way the mount is laid over
the image's filesystem at runtime, so a build-time `chown` is invisible and a
plain `USER app` yields a container that starts, cannot open its SQLite
database, and takes the deployed instance down.

`docker-entrypoint.sh` therefore starts as root, creates the directory holding
`DATABASE_PATH`, takes ownership of it and of the SQLite database and its
sidecars, and drops privileges with `setpriv` before `exec`ing the app.
`setpriv` is part of `util-linux` and already in the Debian base image, so this
adds no package to install, pin, or audit. Run the image with a *non-root*
`--user` / compose `user:` and the entrypoint sees it is not root, skips both
steps, and execs the app directly — the mount then has to already match that
uid. (`--user root` or `--user 0:0` still takes the root path and still drops
to `app`.)

Image scanners flag the missing `USER` (Trivy `DS-0002`, Checkov
`CKV_DOCKER_3`). The check is a heuristic on the instruction rather than on the
process, and the app process is unprivileged. One process genuinely is not:
Docker runs `HEALTHCHECK` outside the entrypoint, so it stays root. It makes an
HTTP request to the app's own port and needs nothing more.

The fixed uid has one visible consequence locally: `./data` in a Compose
checkout ends up owned by 10001 on the host, so inspecting or removing it wants
`sudo`. The comment in `docker-compose.yml` gives the `user:` line that avoids
it.

### Verifying the image locally

CI's `image` job builds the Dockerfile on every pull request, once per release
architecture on a native runner, and runs the build-backend check and
`scripts/verify-privilege-drop.sh` below against each. So the checks here are no
longer the only thing standing between a broken image and a release tag; they
are what to reach for when iterating locally, before pushing.

The release workflow builds `linux/amd64` and `linux/arm64`, so a plain
`docker build` on one machine only exercises half of what ships, and a wheel
published for one architecture and not the other is invisible to it. The
multi-platform build below covers that from a single machine; CI covers it with
one runner per architecture.

```bash
docker build -t later-ink:test .

# Both release architectures. Docker's default driver cannot do multi-platform,
# so this needs a builder with a different driver; --builder targets it for the
# one command instead of changing your default. (Alternatively, enable the
# containerd image store in Docker/OrbStack settings and the default driver
# handles it natively, making the create/rm unnecessary.)
docker buildx create --name later-ink-test
docker buildx build --builder later-ink-test \
  --platform linux/amd64,linux/arm64 --output=type=cacheonly .
docker buildx rm later-ink-test
```

`--output=type=cacheonly` builds without producing an image; the exit code is
the answer. Watch for either build dropping into a *source compile* of `lxml`,
`pillow` or `cryptography` — that may mean no wheel exists for that platform and
Python version, or that the lock carries no hash for the one that does. Either
way it wants investigating with `--progress=plain` to see what the resolver
chose. A healthy build downloads wheels and takes seconds per package.

Two properties worth checking on the built image:

```bash
# The build backend must not have shipped: absent is the healthy result.
docker run --rm later-ink:test pip list | grep -qi hatchling \
  && echo "FAIL: build backend shipped in the runtime image" \
  || echo "ok: build backend absent"

# Spot-check a few runtime pins against requirements.txt.
docker run --rm later-ink:test pip list | grep -Ei 'fastapi|lxml|pillow'
```

The first is the multi-stage property: `grep` exits non-zero when it finds
nothing, so the explicit branch makes the healthy case unambiguous. The second
is a spot check rather than proof — for the full comparison, diff
`docker run --rm later-ink:test pip freeze` against the pins in
`requirements.txt`.

And the privilege drop, against a root-owned volume — the shape Fly presents,
and the failure mode that would otherwise appear only as a crash loop after
deploy:

```bash
./scripts/verify-privilege-drop.sh            # or: ... later-ink:test
```

It creates a scratch volume, forces it root-owned, runs the image against it,
and answers six questions: the container starts and serves `/healthz`, pid 1
runs as uid and gid 10001 across all four identity fields of each (real,
effective, saved and filesystem — a process holding an effective 0 would pass a
check on the real uid alone), `no_new_privs` is set, inheritable capabilities
are empty, the database was created, and `/data` itself — not merely the file
in it — ends up owned by 10001. Each prints its own `ok:`/`FAIL:` verdict and the script exits
non-zero if any failed, so it can gate a release rather than relying on someone
reading a process table and spotting a wrong number. On failure it prints the
container's last log lines, where an unopenable database shows up and nowhere
else.

Scratch resources carry `$$` and are removed by an `EXIT` trap, so a run cannot
collide with a volume that matters (`docker volume create` reuses one of the
same name rather than refusing) and cannot leave one behind. Host port 18080 by
default, to stay clear of a running Compose stack; set `PORT` to change it.

It also prints `docker top` and `/proc/1/status` for the eye. `docker top`
reads the process table on the host rather than in the container, which matters
twice over: the base image has no `ps`, and `docker exec` would run as the
image's user (root, since there is no `USER`) rather than as the process being
checked. Worth re-running whenever the entrypoint or the base image changes.

`--universal` keeps one file valid for both image architectures;
`--python-version 3.11` matches the project's floor rather than whichever
interpreter you happen to be running. The `audit` CI job runs `pip-audit` against
all four locks — pinning freezes known vulnerabilities in place as surely as it
freezes versions, so that job is what makes a stale pin visible.

## Credits

Generated EPUB covers use [League Spartan](https://github.com/theleagueof/league-spartan)
by The League of Moveable Type, bundled under the SIL Open Font License
(`src/later_ink/assets/fonts/OFL.txt`).

## License

MIT
