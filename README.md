# read-later-opds · [later.ink](https://later.ink)

**Your Readwise Reader queue, on your e-reader.**

read-later-opds is a small server that turns your [Readwise Reader](https://readwise.io/read)
account into an [OPDS catalog](https://opds.io/). Point KOReader (or any OPDS
client — Calibre, Moon+ Reader, …) at one URL and browse your saved articles,
downloading any of them as a clean EPUB, generated on the fly from the
article HTML that Reader has already cleaned up.

No plugin, no app, no sync daemon — OPDS is built into KOReader.

**Free and open source (MIT), and built to self-host** with your own Readwise
token. Run it on a NAS, a Raspberry Pi, a VPS, or your laptop — it holds nothing
but your own reading queue.

> **Scope:** later.ink is a *reading path, not a sync path*. It never writes
> back to Readwise, so articles stay in your queue as you read them. If you want
> finished articles archived and removed from the device automatically, the
> [Endle/readwisereader](https://github.com/Endle/readwisereader) KOReader
> plugin does that — this project trades write-back for zero install and working
> on any OPDS client. (Note: Readwise already serves Kindle natively via
> send-to-Kindle; the sweet spot here is Kobo, Boox, and other non-Kindle
> e-ink.)

## Self-host quickstart

```bash
git clone https://github.com/brendanlefebvre/read-later-opds.git
cd read-later-opds
cp .env.example .env
# put your token from https://readwise.io/access_token into .env
docker-compose up -d
```

Your catalog is now at `http://your-host:8000/opds/`.

**KOReader setup:** top menu → magnifying glass → *OPDS catalog* → `+` → enter
the URL. Browse folders (Later, New, Shortlist, Archive, Feed), tap an article
to download and read. Images are embedded in the EPUB, so articles read fully
offline.

**What appears:** articles, emails, PDFs, and books (uploaded EPUBs) — each
delivered as an EPUB, converted from the content Readwise exposes. Tweets,
videos, and podcasts are skipped. Configurable via `READWISE_CATEGORIES`
(e.g. `article,pdf`). Note: long books currently convert to a single
chapter — TOC/chapter-splitting is planned.

## Hosted version

There's no public hosted instance running yet — self-hosting is the supported
path today. The server does include an optional multi-tenant mode (short,
e-ink-typeable catalog URLs like `later.ink/maple-crater-nine/`, with per-user
tokens encrypted at rest), so a free hosted instance may come later. If you want
to stand one up yourself, see the multi-tenant env vars in `.env.example`.

## Architecture

```
src/read_later_opds/
  main.py          # FastAPI routes: OPDS feeds, EPUB downloads, onboarding
  opds.py          # OPDS 1.x Atom feed builder
  epub.py          # HTML → EPUB (ebooklib + lxml cleanup)
  store.py         # SQLite user store + word-based secret URLs
  ratelimit.py     # per-IP throttle for unknown-secret probes
  pages.py         # server-rendered HTML pages
  payments.py      # Stripe verification (optional; inactive unless configured)
  connectors/
    base.py        # Connector interface: folders / articles / article HTML
    readwise.py    # Readwise Reader API v3 connector
```

More connectors (Instapaper, Wallabag, Pocket) are planned — the connector
interface is three methods.

## Development

```bash
pip install -e ".[dev]"
ALLOW_FREE_SIGNUP=1 uvicorn read_later_opds.main:app --reload
pytest
```

## License

MIT
