"""Minimal server-rendered HTML pages. No template engine — f-strings with escaping."""

from html import escape

_STYLE = """
body { font-family: system-ui, sans-serif; line-height: 1.6; max-width: 42em;
       margin: 0 auto; padding: 2em 1em; color: #1a1a1a; }
h1 { font-size: 1.6em; } h2 { font-size: 1.2em; }
code, .url { font-family: ui-monospace, monospace; background: #f2f2f2;
             padding: 0.15em 0.4em; border-radius: 4px; }
.url-big { font-family: ui-monospace, monospace; font-size: 1.3em; background: #f2f2f2;
           padding: 0.6em 0.8em; border-radius: 8px; display: block; margin: 1em 0;
           word-break: break-all; }
.btn { display: inline-block; background: #1a1a1a; color: #fff; padding: 0.6em 1.4em;
       border-radius: 8px; text-decoration: none; border: none; font-size: 1em;
       cursor: pointer; }
input[type=text], input[type=password] { width: 100%; padding: 0.6em; font-size: 1em;
       border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }
.error { color: #b00020; }
.muted { color: #666; font-size: 0.9em; }
ol li { margin: 0.4em 0; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style>"
        f"</head><body>{body}</body></html>"
    )


def landing(payment_link: str | None, free_signup: bool) -> str:
    if payment_link:
        cta = f'<a class="btn" href="{escape(payment_link)}">Get your catalog — pay what you want, $1 min</a>'
    elif free_signup:
        cta = '<a class="btn" href="/start">Get your catalog</a>'
    else:
        cta = '<p class="muted">Signups are not open yet.</p>'
    return _page(
        "Read Later OPDS",
        f"""
<h1>Your Readwise Reader queue, on your e-reader</h1>
<p>read-later-opds turns your <strong>Readwise Reader</strong> account into an
<strong>OPDS catalog</strong>: browse your saved articles from KOReader on any
Kobo, Kindle, or other device, and download them as clean EPUBs. No app to
install &mdash; just add one catalog URL.</p>
{cta}
<h2>How it works</h2>
<ol>
<li>Pay what you want (minimum $1) for early access</li>
<li>Paste your Readwise access token</li>
<li>Type your personal catalog URL into KOReader once</li>
<li>Read your queue, offline, on e-ink</li>
</ol>
<h2>FAQ</h2>
<p><strong>Is my token safe?</strong> Your token is stored server-side, sent only
to Readwise over HTTPS, never logged, and used solely to fetch your articles.
You can delete it (and everything else we hold) with one click.</p>
<p><strong>Can I self-host instead?</strong> Yes &mdash; the entire server is
open source (MIT). Run it yourself with docker-compose and skip the hosted
version entirely.</p>
""",
    )


def start_form(session_id: str | None, error: str | None = None) -> str:
    err = f'<p class="error">{escape(error)}</p>' if error else ""
    hidden = (
        f'<input type="hidden" name="session_id" value="{escape(session_id)}">'
        if session_id
        else ""
    )
    return _page(
        "Set up your catalog",
        f"""
<h1>Set up your catalog</h1>
{err}
<p>Paste your Readwise access token. Get it at
<a href="https://readwise.io/access_token">readwise.io/access_token</a>.</p>
<form method="post" action="/start">
{hidden}
<p><input type="password" name="readwise_token" placeholder="Readwise access token" required></p>
<p><button class="btn" type="submit">Create my catalog</button></p>
</form>
<p class="muted">Your token is stored server-side, never logged, and only ever
sent to Readwise.</p>
""",
    )


def success(catalog_url: str, secret: str) -> str:
    return _page(
        "Your catalog is ready",
        f"""
<h1>Your catalog is ready</h1>
<p>Type this URL into KOReader (it's designed to be easy to type on an e-ink
keyboard &mdash; all lowercase, no symbols except hyphens and slashes):</p>
<span class="url-big">{escape(catalog_url)}</span>
<h2>KOReader setup</h2>
<ol>
<li>Open KOReader &rarr; tap the top menu &rarr; magnifying glass icon</li>
<li>Choose <strong>OPDS catalog</strong></li>
<li>Tap <strong>+</strong> and add the URL above (no username/password needed)</li>
<li>Browse your queue and tap any article to download it as EPUB</li>
</ol>
<p><strong>Keep this URL private</strong> &mdash; anyone who has it can read
your saved articles. Lost or leaked it?</p>
<form method="post" action="/{escape(secret)}/regenerate" style="display:inline">
<button class="btn" type="submit">Get a new URL</button>
</form>
<form method="post" action="/{escape(secret)}/delete" style="display:inline; margin-left:0.5em"
      onsubmit="return confirm('Delete your catalog and stored token?')">
<button class="btn" type="submit" style="background:#b00020">Delete everything</button>
</form>
""",
    )


def deleted() -> str:
    return _page(
        "Deleted",
        """
<h1>All gone</h1>
<p>Your catalog and stored Readwise token have been deleted.</p>
""",
    )
