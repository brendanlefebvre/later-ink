"""Minimal server-rendered HTML pages. No template engine — f-strings with escaping.

Visual direction: the whole site is framed as an e-ink reader in night mode —
a slate/black reading column with a status bar and footer for chrome, soft white
text, and a single warm "frontlight" amber reserved for primary actions. Display
type is League Spartan (served from /assets, the same face used on EPUB covers);
body copy is set in a system serif to reinforce that this is about reading.
"""

import time
from html import escape

REPO_URL = "https://github.com/brendanlefebvre/later-ink"

_STYLE = """
@font-face {
  font-family: "League Spartan";
  src: url(/assets/fonts/league-spartan.ttf) format("truetype");
  font-weight: 100 900;
  font-display: swap;
}

:root {
  --desk:    #08090b;
  --bg:      #0d0f12;
  --surface: #14171b;
  --line:    #262b33;
  --text:    #f4f5f7;
  --dim:     #c6ccd4;
  --muted:   #8b94a1;
  --faint:   #626b78;
  --accent:      #d8b880;
  --accent-soft: #c9a875;
  --danger:      #e6675f;
  --shadow: rgba(0,0,0,.8);
  --glow: rgba(216,184,128,.10);
  --col: 44em;
  --spartan: "League Spartan", "Arial Narrow", sans-serif;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
}

/* Light theme = "e-ink day mode": near-black text on light gray. The amber button
   stays; --accent-soft deepens to bronze so it's legible on light, and the frontlight
   glow is dropped. Comes from the OS setting unless the toggle forces dark, or is
   forced on by the toggle. (Both selectors carry the same tokens by design.) */
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --desk:#e6e7ea; --bg:#f5f6f7; --surface:#eceef1; --line:#d7dade;
    --text:#17191d; --dim:#3d434b; --muted:#5f6570; --faint:#9aa0aa;
    --accent-soft:#8a6a2f; --danger:#c0392b;
    --shadow: rgba(20,24,31,.14); --glow: transparent;
  }
}
:root[data-theme="light"] {
  --desk:#e6e7ea; --bg:#f5f6f7; --surface:#eceef1; --line:#d7dade;
  --text:#17191d; --dim:#3d434b; --muted:#5f6570; --faint:#9aa0aa;
  --accent-soft:#8a6a2f; --danger:#c0392b;
  --shadow: rgba(20,24,31,.14); --glow: transparent;
}

* { box-sizing: border-box; }
html, body { background: var(--desk); margin: 0; }

.device {
  background: var(--bg);
  color: var(--text);
  max-width: var(--col);
  min-height: 100vh;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--line);
  border-right: 1px solid var(--line);
  box-shadow: 0 0 80px -20px var(--shadow);
  font-family: var(--serif);
  font-size: 18px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}

/* ---- status bar / footer (the e-reader chrome) ---- */
.statusbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: .85rem 1.6rem;
  border-bottom: 1px solid var(--line);
  font-family: var(--spartan);
  font-size: .72rem;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--muted);
}
.wordmark { color: var(--text); font-weight: 700; letter-spacing: .16em; text-decoration: none; }
.wordmark .dot { color: var(--accent-soft); }
.status { display: flex; align-items: center; gap: .8rem; }
.wifi { display: block; color: var(--muted); }
.clock { font-variant-numeric: tabular-nums; }
.theme-toggle { -webkit-appearance: none; appearance: none; background: none; border: 0;
  padding: 0; margin: 0; cursor: pointer; color: var(--muted); line-height: 0;
  display: inline-flex; align-items: center; }
.theme-toggle:hover { color: var(--text); }
.theme-toggle svg { display: block; }
/* the icon shows the ACTIVE theme: moon in dark, sun in light */
.theme-toggle .sun { display: none; }
.theme-toggle .moon { display: block; }
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) .theme-toggle .moon { display: none; }
  :root:not([data-theme="dark"]) .theme-toggle .sun { display: block; }
}
:root[data-theme="light"] .theme-toggle .moon { display: none; }
:root[data-theme="light"] .theme-toggle .sun { display: block; }
.battery { display: inline-flex; align-items: center; gap: 4px; }
.battery .cell {
  width: 22px; height: 11px; border: 1px solid var(--muted);
  border-radius: 2px; position: relative; padding: 1.5px;
}
.battery .cell::after {
  content: ""; position: absolute; right: -3px; top: 3px;
  width: 2px; height: 5px; background: var(--muted); border-radius: 0 1px 1px 0;
}
.battery .fill { display: block; height: 100%; width: 88%; background: var(--dim); border-radius: 1px; }

.footerbar { margin-top: auto; border-top: 1px solid var(--line); padding: 1.3rem 1.6rem 1.6rem; }
.progress { height: 2px; background: var(--line); border-radius: 2px; overflow: hidden; margin-bottom: .8rem; }
.progress span { display: block; height: 100%; width: 100%;
  background: linear-gradient(90deg, var(--faint), var(--accent-soft)); }
.footmeta {
  display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap;
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .15em;
  font-size: .66rem; color: var(--muted);
}
.footmeta a { color: var(--muted); text-decoration: none; }
.footmeta a:hover { color: var(--text); }

/* ---- screen ---- */
.screen { flex: 1; padding: 0 1.6rem 2.4rem; position: relative; }
.screen::before {
  content: ""; position: absolute; inset: 0 0 auto 0; height: 420px;
  background: radial-gradient(120% 80% at 50% -10%, var(--glow), transparent 60%);
  pointer-events: none;
}

/* ---- typography ---- */
h1 {
  font-family: var(--spartan);
  font-weight: 800;
  font-size: 2rem;
  line-height: 1.05;
  letter-spacing: -.01em;
  margin: 2.4rem 0 1rem;
}
h2 { font-family: var(--spartan); font-size: 1.2em; }
p { margin: 0 0 1rem; }
p.small { color: var(--dim); }
.muted { color: var(--muted); font-size: .95rem; }
.error { color: var(--danger); }

a { color: var(--accent-soft); text-decoration: underline; text-underline-offset: 2px; }
a:hover { color: var(--accent); }
a.inline { text-decoration: none; border-bottom: 1px solid var(--accent-soft); }
a.inline:hover { border-color: var(--accent-soft); }

code, .url {
  font-family: var(--mono); font-size: .88em;
  background: var(--surface); border: 1px solid var(--line);
  padding: .1em .4em; border-radius: 4px; color: var(--text); word-break: break-word;
}
.url-big {
  font-family: var(--mono); font-size: 1.2em; background: var(--surface);
  border: 1px solid var(--line); color: var(--text);
  padding: .7em .9em; border-radius: 8px; display: block; margin: 1.2em 0;
  word-break: break-all;
}

/* ---- controls ---- */
.btn {
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .09em;
  font-weight: 700; font-size: .82rem; text-decoration: none;
  display: inline-block; padding: .95em 1.7em; border-radius: 3px;
  border: none; cursor: pointer;
  background: var(--accent); color: #16130d;
  box-shadow: 0 0 0 1px rgba(216,184,128,.35), 0 10px 40px -10px rgba(216,184,128,.55);
  transition: background .15s ease, box-shadow .15s ease;
}
.btn:hover { background: #e7ca97; color: #16130d;
  box-shadow: 0 0 0 1px rgba(216,184,128,.5), 0 12px 46px -8px rgba(216,184,128,.7); }
.btn-danger {
  background: transparent; color: var(--danger);
  box-shadow: inset 0 0 0 1px rgba(230,103,95,.5);
}
.btn-danger:hover { background: rgba(230,103,95,.12); color: var(--danger); box-shadow: inset 0 0 0 1px var(--danger); }
.link-ghost {
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .09em;
  font-size: .78rem; font-weight: 600; text-decoration: none;
  color: var(--dim); border-bottom: 1px solid var(--faint); padding-bottom: 2px;
  transition: color .15s ease, border-color .15s ease;
}
.link-ghost:hover { color: var(--text); border-color: var(--accent-soft); }

input[type=text], input[type=password] {
  width: 100%; padding: .75em .8em; font-size: 1em; font-family: var(--serif);
  background: var(--surface); color: var(--text);
  border: 1px solid var(--line); border-radius: 6px; box-sizing: border-box;
}
input::placeholder { color: var(--faint); }
input:focus { outline: none; border-color: var(--accent-soft); box-shadow: 0 0 0 3px rgba(201,168,117,.15); }

ol { padding-left: 1.4em; }
ol li { margin: .45em 0; color: var(--dim); }

/* ---- landing: hero ---- */
.hero { padding: 3.4rem 0 2.6rem; }
.hero h1 {
  font-size: clamp(2.6rem, 8.5vw, 4.3rem);
  line-height: .98; letter-spacing: -.015em;
  margin: 0 0 1.2rem; text-wrap: balance;
}
.hero h1 .end { color: var(--accent-soft); }
.lede { font-size: 1.2rem; line-height: 1.6; color: var(--dim); margin: 0 0 2rem; max-width: 33em; }
.lede strong { color: var(--text); font-weight: 600; }
.hosted-note { color: var(--muted); max-width: 34em; margin: 0 0 1.4rem; }
.cta-row { display: flex; gap: 1.3rem; align-items: center; flex-wrap: wrap; }

/* landing: demo recording — a bright "screen" playing against the dark page */
.demo { margin: 2.8rem auto 0; max-width: 320px; }
.demo img { width: 100%; height: auto; display: block; border: 1px solid var(--line);
  border-radius: 10px; box-shadow: 0 18px 50px -18px var(--shadow); }
.demo figcaption { font-family: var(--spartan); text-transform: uppercase;
  letter-spacing: .16em; font-size: .66rem; color: var(--muted); text-align: center;
  margin-top: .85rem; }

/* ---- landing: section labels + blocks ---- */
.eyebrow {
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .22em;
  font-size: .72rem; font-weight: 700; color: var(--muted);
  margin: 3.2rem 0 1.1rem; padding-top: 2rem; border-top: 1px solid var(--line);
}
.targets { display: grid; grid-template-columns: 1fr 1fr; column-gap: 2rem; }
.target { padding: 1rem 0; border-bottom: 1px solid var(--line); }
.target h3 {
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .13em;
  font-size: .7rem; color: var(--muted); margin: 0 0 .35rem; font-weight: 700;
}
.target p { margin: 0; font-size: 1rem; color: var(--dim); }
.targets + p.small { margin-top: 1.2rem; color: var(--muted); }

ol.steps { list-style: none; counter-reset: step; padding: 0; margin: 0; }
ol.steps li {
  counter-increment: step; position: relative;
  padding: .85rem 0 .85rem 3rem; border-bottom: 1px solid var(--line);
  color: var(--dim); margin: 0;
}
ol.steps li::before {
  content: counter(step, decimal-leading-zero);
  position: absolute; left: 0; top: .95rem;
  font-family: var(--spartan); font-weight: 700; font-size: .95rem;
  letter-spacing: .04em; color: var(--faint);
}

details { border-bottom: 1px solid var(--line); padding: .3rem 0; }
details summary {
  cursor: pointer; list-style: none; padding: .85rem 1.8rem .85rem 0;
  font-family: var(--spartan); text-transform: uppercase; letter-spacing: .06em;
  font-size: .82rem; font-weight: 700; color: var(--text); position: relative;
}
details summary::-webkit-details-marker { display: none; }
details summary::after {
  content: "+"; position: absolute; right: .2rem; top: .7rem;
  font-family: var(--spartan); font-size: 1.2rem; color: var(--faint);
}
details[open] summary::after { content: "–"; }
details .answer { padding: 0 0 1rem; color: var(--dim); font-size: 1.02rem; }

/* ---- focus + motion ---- */
a:focus-visible, .btn:focus-visible, summary:focus-visible, input:focus-visible,
.theme-toggle:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px;
}
@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
.hero h1, .hero .lede, .hero .cta-row { animation: rise .7s cubic-bezier(.2,.7,.2,1) both; }
.hero .lede { animation-delay: .1s; }
.hero .cta-row { animation-delay: .2s; }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }

@media (max-width: 640px) {
  .device { font-size: 17px; border-left: none; border-right: none; }
  .screen { padding: 0 1.2rem 2rem; }
  .statusbar, .footerbar { padding-left: 1.2rem; padding-right: 1.2rem; }
  .targets { grid-template-columns: 1fr; column-gap: 0; }
}
"""

_STATUSBAR = (
    '<header class="statusbar">'
    '<a class="wordmark" href="/">LATER<span class="dot">.</span>INK</a>'
    '<span class="status">'
    '<svg class="wifi" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" '
    'aria-hidden="true"><path d="M12 18a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM12 4C7.3 4 3.1 5.9 0 9l2.1 '
    "2.1C4.6 8.6 8.1 7 12 7s7.4 1.6 9.9 4.1L24 9c-3.1-3.1-7.3-5-12-5zm0 6c-2.9 0-5.5 1.2-7.4 "
    '3.1L6.7 15.2C8.1 13.8 9.9 13 12 13s3.9.8 5.3 2.2l2.1-2.1C17.5 11.2 14.9 10 12 10z"/></svg>'
    '<span class="battery"><span class="cell"><span class="fill"></span></span></span>'
    '<span class="clock" id="ll-clock"></span>'
    '<button class="theme-toggle" type="button" aria-label="Switch between light and dark">'
    '<svg class="moon" viewBox="0 0 24 24" width="15" height="15" fill="currentColor" '
    'aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
    '<svg class="sun" viewBox="0 0 24 24" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M4.4 4.4l1.6 1.6'
    'M18 18l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.4 19.6l1.6-1.6M18 6l1.6-1.6"/></svg>'
    "</button>"
    "</span></header>"
)

# Applied in <head> before first paint so a saved override doesn't flash the wrong theme.
_THEME_HEAD_SCRIPT = (
    "<script>try{var t=localStorage.getItem('ll-theme');"
    "if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>"
)

# Toggle: flip the effective theme and remember it (overrides the OS setting).
_THEME_TOGGLE_SCRIPT = (
    "<script>(function(){var r=document.documentElement,"
    "b=document.querySelector('.theme-toggle');if(!b)return;"
    "b.addEventListener('click',function(){var c=r.getAttribute('data-theme');"
    "if(!c)c=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';"
    "var n=c==='dark'?'light':'dark';r.setAttribute('data-theme',n);"
    "try{localStorage.setItem('ll-theme',n)}catch(e){}});}());</script>"
)

# The status-bar clock mirrors the viewer's own device time (like a real e-reader),
# so it must be set client-side; server time would be UTC on the host.
_CLOCK_SCRIPT = (
    "<script>(function(){var e=document.getElementById('ll-clock');if(!e)return;"
    "function t(){e.textContent=new Date().toLocaleTimeString([],"
    "{hour:'2-digit',minute:'2-digit'});}t();setInterval(t,15000);}());</script>"
)

_FOOTERBAR = (
    '<footer class="footerbar">'
    '<div class="progress"><span></span></div>'
    '<div class="footmeta">'
    "<span>Later.Ink &middot; Free &amp; open source &middot; "
    f'<a href="{REPO_URL}">GitHub</a></span>'
    "<span>Page 1 of 1 &middot; 100%</span>"
    "</div></footer>"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        # Never emit a Referer from any served page: catalog pages live under
        # /{secret}/, so the wordmark and footer links would otherwise leak the
        # secret (to / and to GitHub) via the Referer header.
        '<meta name="referrer" content="no-referrer">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style>"
        f"{_THEME_HEAD_SCRIPT}"
        "</head><body>"
        f'<div class="device">{_STATUSBAR}'
        f'<main class="screen">{body}</main>'
        f"{_FOOTERBAR}</div>"
        f"{_CLOCK_SCRIPT}{_THEME_TOGGLE_SCRIPT}"
        "</body></html>"
    )


def landing(payment_link: str | None, free_signup: bool) -> str:
    if payment_link:
        cta = f'<a class="btn" href="{escape(payment_link)}">Get your catalog</a>'
        ghost = '<a class="link-ghost" href="#self-host">Or self-host it &darr;</a>'
        note = ""
    elif free_signup:
        cta = '<a class="btn" href="/start">Get your catalog</a>'
        ghost = '<a class="link-ghost" href="#self-host">Or self-host it &darr;</a>'
        note = ""
    else:
        # No hosted instance yet — self-hosting is what we offer, so it's the primary CTA.
        cta = '<a class="btn" href="#self-host">Self-host it</a>'
        ghost = f'<a class="link-ghost" href="{REPO_URL}">View on GitHub &rarr;</a>'
        note = (
            '<p class="hosted-note">No hosted instance yet &mdash; run it yourself in a few '
            "minutes. A hosted option may come later.</p>"
        )
    return _page(
        "Later.Ink — your read-later queue, on e-ink",
        f"""
<section class="hero">
  <h1>Your read-later queue,<br>on e-ink<span class="end">.</span></h1>
  <p class="lede">Later.Ink turns your <strong>Readwise Reader</strong> library into an
    <strong>OPDS catalog</strong> &mdash; open it in KOReader on a Kobo or Boox, or in a
    reader app like Fablum on your <strong>iPhone or iPad</strong>. Every item is fetched as a clean EPUB,
    images and all, that reads fully offline.</p>
  {note}
  <div class="cta-row">{cta}{ghost}</div>
</section>

<figure class="demo">
  <img src="/assets/demo.gif" width="480" height="596" loading="lazy"
    alt="Browsing the Later.Ink catalog in KOReader, downloading a saved article, and reading it as an EPUB with images.">
  <figcaption>Live in KOReader — browse, download, read</figcaption>
</figure>

<h2 class="eyebrow">Works with your reader</h2>
<div class="targets">
  <div class="target"><h3>iPhone &amp; iPad</h3><p>Fablum, justRead, or PocketBook</p></div>
  <div class="target"><h3>E-ink</h3><p>KOReader on Kobo, Boox &amp; more</p></div>
  <div class="target"><h3>Desktop</h3><p>Thorium Reader</p></div>
  <div class="target"><h3>Android</h3><p>Moon+ Reader, KOReader</p></div>
</div>
<p class="small">Anything that speaks OPDS. There's no app from us to install &mdash;
  you add one catalog URL and your queue is there.</p>

<h2 class="eyebrow">What lands in your library</h2>
<p class="small">Articles, newsletters, PDFs, books you've uploaded, video transcripts,
  tweet threads, and podcasts &mdash; each delivered as an EPUB your reader can open.
  (For a podcast, load its transcript in Readwise Reader first.)</p>

<h2 class="eyebrow" id="self-host">Self-host it</h2>
<p class="small">Free and open source (MIT). Bring your own Readwise token &mdash;
  Later.Ink stores nothing.</p>
<ol class="steps">
  <li>Clone <a class="inline" href="{REPO_URL}">the repository</a></li>
  <li>Put your <a class="inline" href="https://readwise.io/access_token">Readwise token</a> in a <code>.env</code> file</li>
  <li>Run <code>docker compose up</code> &mdash; your catalog is at <code>/opds/</code></li>
  <li>Add that URL to your reader and start reading</li>
</ol>

<h2 class="eyebrow">Questions</h2>
<details>
  <summary>Why OPDS?</summary>
  <div class="answer">It's the open standard your reader already speaks. No plugin, no
    sideloading, nothing to install beyond typing a URL.</div>
</details>
<details>
  <summary>Do you see my articles?</summary>
  <div class="answer">No &mdash; we keep no copy. Your articles are fetched from Readwise and
    turned into EPUBs on the fly, then streamed straight to your reader; nothing is stored on
    the server. The only thing saved is your Readwise token, encrypted at rest and never logged.
    Self-hosted, it never leaves your machine at all.</div>
</details>
<details>
  <summary>Other read-later services?</summary>
  <div class="answer">The connector interface is three methods. Instapaper and Wallabag are
    on the roadmap &mdash; contributions welcome on <a class="inline" href="{REPO_URL}">GitHub</a>.</div>
</details>
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
<p class="small">Paste your Readwise access token. Get it at
<a class="inline" href="https://readwise.io/access_token">readwise.io/access_token</a>.</p>
<form method="post" action="/start">
{hidden}
<p><input type="password" name="readwise_token" placeholder="Readwise access token" required></p>
<p><button class="btn" type="submit">Create my catalog</button></p>
</form>
<p class="muted">Your token is encrypted at rest with a key held outside the
database, never logged, and only ever sent to Readwise.</p>
""",
    )


def success(catalog_url: str, secret: str, csrf: str) -> str:
    return _page(
        "Your catalog is ready",
        f"""
<h1>Your catalog is ready</h1>
<p class="small">Type this URL into KOReader (it's designed to be easy to type on an e-ink
keyboard &mdash; all lowercase, no symbols except hyphens and slashes):</p>
<span class="url-big">{escape(catalog_url)}</span>
<h2 class="eyebrow">KOReader setup</h2>
<ol>
<li>Open KOReader &rarr; tap the top menu &rarr; magnifying glass icon</li>
<li>Choose <strong>OPDS catalog</strong></li>
<li>Tap <strong>+</strong> and add the URL above (no username/password needed)</li>
<li>Browse your queue and tap any article to download it as EPUB</li>
</ol>
<p class="muted">Note: the catalog shows articles, emails, PDFs, books, video
transcripts, tweet threads, and podcasts (load a podcast's transcript in
Reader first).</p>
<p class="small"><strong>Keep this URL private</strong> &mdash; anyone who has it can read
your saved articles. Lost or leaked it? <strong>Save this page</strong> &mdash;
the buttons below only work from here.</p>
<form method="post" action="/{escape(secret)}/regenerate" style="display:inline">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<button class="btn" type="submit">Get a new URL</button>
</form>
<form method="post" action="/{escape(secret)}/delete" style="display:inline; margin-left:0.5em"
      onsubmit="return confirm('Delete your catalog and stored token?')">
<input type="hidden" name="csrf" value="{escape(csrf)}">
<button class="btn btn-danger" type="submit">Delete everything</button>
</form>
""",
    )


def deleted() -> str:
    return _page(
        "Deleted",
        """
<h1>All gone</h1>
<p class="small">Your catalog and stored Readwise token have been deleted.</p>
""",
    )


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


# Minimal standalone admin page — not the themed device chrome; token-gated in main.
_STATS_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:52em;margin:2.5em auto;padding:0 1em;"
    "color:#1a1a1a;line-height:1.5}h1{font-size:1.3em}h2{font-size:1em;margin-top:1.8em}"
    "table{border-collapse:collapse;width:100%;margin:.6em 0}th,td{text-align:left;"
    "padding:.35em .6em;border-bottom:1px solid #eee;font-size:.92em;vertical-align:top}"
    "td.n{font-variant-numeric:tabular-nums;font-weight:600;width:4em}"
    ".muted{color:#888}.ua{color:#999;font-size:.82em;word-break:break-all}"
    "td.ref{word-break:break-all}"
)


def stats_page(total: int, total_30d: int, referrers, recent) -> str:
    ref_rows = "".join(
        f'<tr><td class="n">{n}</td><td class="ref">{escape(ref)}</td></tr>'
        for ref, n in referrers
    ) or '<tr><td colspan="2" class="muted">No hits yet.</td></tr>'
    recent_rows = "".join(
        f"<tr><td>{escape(_fmt_ts(ts))}</td>"
        f'<td class="ref">{escape(ref or "(direct)")}</td>'
        f'<td class="ua">{escape((ua or "")[:90])}</td></tr>'
        for ts, _path, ref, ua in recent
    ) or '<tr><td colspan="3" class="muted">—</td></tr>'
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Later.Ink — traffic</title><style>{_STATS_STYLE}</style></head><body>"
        "<h1>Later.Ink traffic</h1>"
        f"<p><strong>{total}</strong> total hits · <strong>{total_30d}</strong> in the "
        "last 30 days. (Landing-page views only; referer + user-agent, no IPs.)</p>"
        f"<h2>Top referrers · last 30 days</h2><table>{ref_rows}</table>"
        f"<h2>Recent hits</h2><table>"
        "<tr><th>When (UTC)</th><th>Referrer</th><th>User-agent</th></tr>"
        f"{recent_rows}</table></body></html>"
    )
