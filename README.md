# read-later-opds

**Your Readwise Reader queue, on your e-reader.**

read-later-opds is a small server that turns your [Readwise Reader](https://readwise.io/read)
account into an [OPDS catalog](https://opds.io/). Point KOReader (or any OPDS
client — Calibre, Moon+ Reader, …) at one URL and browse your saved articles,
downloading any of them as a clean EPUB, generated on the fly from the
article HTML that Reader has already cleaned up.

No plugin, no app, no sync daemon — OPDS is built into KOReader.

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
the URL. Browse folders (Later, New, Shortlist, Archive), tap an article to
download and read.

## Hosted version

Don't want to run a server? There's a hosted version — pay what you want
($1 minimum, one-time, early access). You paste your Readwise token, we give
you a short personal catalog URL designed to be easy to type on an e-ink
keyboard.

## Architecture

```
src/read_later_opds/
  main.py          # FastAPI routes: OPDS feeds, EPUB downloads, onboarding
  opds.py          # OPDS 1.x Atom feed builder
  epub.py          # HTML → EPUB (ebooklib + lxml cleanup)
  store.py         # SQLite user store + word-based secret URLs
  ratelimit.py     # per-IP throttle for unknown-secret probes
  pages.py         # server-rendered HTML pages
  payments.py      # Stripe Checkout Session verification
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
