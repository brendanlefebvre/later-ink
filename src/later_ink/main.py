import hashlib
import hmac
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import quote

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import __version__, config, opds, pages
from .connectors import readwise
from .connectors.base import ArticleUnavailable, Connector, UpstreamError
from .connectors.readwise import ReadwiseConnector
from .connectors.wallabag import WallabagConnector
from .epub import build_epub
from .payments import verify_checkout_session
from .ratelimit import DurableLimiter, MemoryLimiter
from .store import RESERVED_PATHS, Store

logger = logging.getLogger(__name__)

# Must run before anything reads config: fills os.environ from
# ~/.config/later-ink/env (or ./.env) unless the vars are already set.
config.load_env_file()

NAV_MEDIA = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_MEDIA = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH_MEDIA = "application/opensearchdescription+xml"

MAX_TENANT_CONNECTORS = 200

# Single-user (self-host) connectors, keyed by connector name
_connectors: dict[str, Connector] = {}
# Multi-tenant connector cache, keyed by Readwise token
_tenant_connectors: OrderedDict[str, ReadwiseConnector] = OrderedDict()


def _derive_csrf_key(encryption_key: str) -> bytes:
    """Separate CSRF signing key from the same root secret.

    The CSRF HMAC used to be keyed with the raw Fernet key. That worked, but it
    tied two unrelated rotation stories together: rotating the token-encryption
    key to respond to a suspected disclosure would also have invalidated every
    outstanding CSRF token, and vice versa. HKDF gives each purpose its own key
    with no extra config for the operator.
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"later-ink/csrf/v1"
    ).derive(encryption_key.encode())


@asynccontextmanager
async def lifespan(app: FastAPI):
    key = config.get_encryption_key()
    ephemeral = key is None
    if ephemeral:
        # Fail closed. An ephemeral key silently destroys every stored token on
        # the next restart, and the instance looks healthy the whole time — the
        # operator finds out when all their users' catalogs 404 at once. A
        # single-user self-host instance stores no tokens, so there it's fine.
        if config.signups_enabled():
            raise RuntimeError(
                "ENCRYPTION_KEY must be set when signups are enabled "
                "(ALLOW_FREE_SIGNUP or STRIPE_SECRET_KEY): without it, stored "
                "Readwise tokens become unreadable on restart and every user "
                "has to re-onboard. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        key = Fernet.generate_key().decode()

    try:
        fernet = Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            "ENCRYPTION_KEY is not a valid Fernet key (expected 32 url-safe "
            "base64-encoded bytes)"
        ) from e

    app.state.csrf_key = _derive_csrf_key(key)
    app.state.store = Store(config.get_database_path(), fernet)
    if ephemeral:
        # Signups may have been turned off after the fact; the tokens already in
        # the database are just as lost.
        if app.state.store.user_count():
            raise RuntimeError(
                "ENCRYPTION_KEY is not set but the database already has users — "
                "their stored tokens cannot be decrypted without the original key."
            )
        logger.warning(
            "ENCRYPTION_KEY is not set — using an ephemeral key. Fine for "
            "single-user self-hosting, which stores no tokens; set it before "
            "enabling signups."
        )
    # Unknown-secret probes and signups both need to survive a cold start, so
    # they're durable; feed traffic is throttled in-process — see ratelimit.py.
    app.state.limiter = DurableLimiter(
        app.state.store, bucket="miss", limit=20, window=3600.0
    )
    app.state.signup_limiter = DurableLimiter(
        app.state.store,
        bucket="signup",
        limit=config.get_signup_rate_limit(),
        window=3600.0,
    )
    app.state.feed_limiter = MemoryLimiter(
        limit=config.get_feed_rate_limit(), window=60.0
    )
    token = config.get_readwise_token()
    if token:
        _connectors["readwise"] = ReadwiseConnector(token, config.get_readwise_categories())
    wallabag_cfg = config.get_wallabag_config()
    if wallabag_cfg:
        _connectors["wallabag"] = WallabagConnector(**wallabag_cfg)
    yield
    for c in _connectors.values():
        if hasattr(c, "close"):
            await c.close()
    _connectors.clear()
    for c in _tenant_connectors.values():
        await c.close()
    _tenant_connectors.clear()


app = FastAPI(
    title="Later.Ink",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# Paths whose responses are the same for everyone and safe for a shared cache.
# Everything else is either a private catalog, a page carrying a secret, or an
# authenticated view, so the default below is no-store — deny by default, since
# forgetting to list a new private route is the dangerous direction to fail.
_PUBLIC_PATHS = frozenset({"/", "/health", "/healthz", "/version"})


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/assets/")


def _is_https(request: Request) -> bool:
    if config.trust_proxy_headers():
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            return proto.split(",")[0].strip() == "https"
    return request.url.scheme == "https"


_RETRY_AFTER_FEED = "60"
_RETRY_AFTER_SIGNUP = "3600"


def _too_many(retry_after: str) -> Response:
    # Plain text with Retry-After: KOReader surfaces the body on a failed
    # download, and a polite client can back off instead of retrying blind.
    return Response(
        content="Too many requests; try again later\n",
        status_code=429,
        media_type="text/plain",
        headers={"Retry-After": retry_after},
    )


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Throttle signups and catalog traffic per IP.

    Applied here rather than per-handler so a route added later is covered by
    default — the same reason the cache policy below works off an allowlist.
    Public paths (landing, assets, health) are exempt: they touch no upstream
    and serving them is the point.

    Note this is deliberately not the defence against guessing catalog
    secrets. That one lives in _resolve_secret, because it can only count a
    request *after* the lookup has shown the secret was wrong.
    """
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    ip = _client_ip(request)
    if path == "/start":
        limiter: DurableLimiter = app.state.signup_limiter
        if limiter.blocked(ip):
            return _too_many(_RETRY_AFTER_SIGNUP)
        limiter.record(ip)
    elif not app.state.feed_limiter.allow(ip):
        return _too_many(_RETRY_AFTER_FEED)

    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Hardening headers, and cache policy for anything user-specific.

    Fly terminates TLS and sets HSTS at the edge, but the app is also run
    behind other proxies and bare, so it sets its own.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if _is_https(request):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", pages.CSP)
    if not _is_public(request.url.path):
        # Overwrite rather than setdefault: a catalog feed or a page showing a
        # secret must not sit in a proxy cache, whatever the handler asked for.
        response.headers["Cache-Control"] = "private, no-store"
    return response


async def _tenant_connector(token: str) -> ReadwiseConnector:
    conn = _tenant_connectors.get(token)
    if conn is None:
        conn = ReadwiseConnector(token)
        _tenant_connectors[token] = conn
        while len(_tenant_connectors) > MAX_TENANT_CONNECTORS:
            _, evicted = _tenant_connectors.popitem(last=False)
            await evicted.close()
    _tenant_connectors.move_to_end(token)
    return conn


def _client_ip(request: Request) -> str:
    # Forwarding headers are client-controlled unless a trusted proxy sets
    # them; honoring them blindly hands out a fresh rate-limit bucket per
    # request. TRUST_PROXY_HEADERS is set on Fly, off by default elsewhere.
    if config.trust_proxy_headers():
        fwd = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _csrf_token(secret: str) -> str:
    return hmac.new(app.state.csrf_key, f"csrf:{secret}".encode(), "sha256").hexdigest()[:32]


def _require_csrf(secret: str, token: str | None) -> None:
    # Encode both sides: a non-ASCII submitted token would otherwise make
    # hmac.compare_digest raise TypeError (500) instead of a clean 403.
    if not token or not hmac.compare_digest(
        _csrf_token(secret).encode("utf-8"), token.encode("utf-8")
    ):
        raise HTTPException(403, "Invalid or missing CSRF token")


def _resolve_secret(secret: str, request: Request) -> str:
    """Map a URL secret to a Readwise token, rate-limiting unknown-secret probes."""
    if secret in RESERVED_PATHS:
        raise HTTPException(404)
    limiter: DurableLimiter = app.state.limiter
    ip = _client_ip(request)
    if limiter.blocked(ip):
        raise HTTPException(429, "Too many attempts; try again later")
    token = app.state.store.get_token(secret)
    if token is None:
        limiter.record(ip)
        raise HTTPException(404, "Unknown catalog")
    return token


def _feed_id(secret: str) -> str:
    # Stable per-user feed id that doesn't echo the secret itself
    return "urn:later-ink:u:" + hashlib.sha1(secret.encode()).hexdigest()[:12]


@app.exception_handler(UpstreamError)
async def upstream_error_handler(request: Request, exc: UpstreamError):
    # KOReader shows response text on failed downloads — keep it readable.
    return Response(content=str(exc), status_code=502, media_type="text/plain")


@app.exception_handler(ArticleUnavailable)
async def article_unavailable_handler(request: Request, exc: ArticleUnavailable):
    return Response(content=str(exc), status_code=exc.status, media_type="text/plain")


# ---------------------------------------------------------------- pages


def _write_landing_hit(store: Store, referer: str | None, user_agent: str | None) -> None:
    # Runs after the response is sent (BackgroundTasks): the SQLite write must
    # never sit in the landing render path, or a Show-HN spike serializes views
    # behind the writer. Never let analytics break anything — swallow errors.
    try:
        store.record_hit("/", referer, user_agent, config.get_stats_retention_days())
    except Exception:
        logger.debug("referrer-log write failed", exc_info=True)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, background_tasks: BackgroundTasks):
    # Opt-in referrer log (only when STATS_TOKEN is set). Capture the headers
    # now, but defer the write off the request path. No IP is stored.
    if config.get_stats_token():
        ua = request.headers.get("user-agent")
        background_tasks.add_task(
            _write_landing_hit,
            app.state.store,
            request.headers.get("referer"),
            ua[:300] if ua else None,
        )
    return pages.landing(
        config.get_stripe_payment_link(),
        config.allow_free_signup(),
        config.get_base_url(),
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, token: str = Query(default="")):
    expected = config.get_stats_token()
    # Prefer the header: a token in the query string ends up in access logs,
    # browser history, and any Referer the page emits. ?token= stays supported
    # because it's what you can type on a device without a curl.
    auth = request.headers.get("authorization", "")
    presented = auth[7:].strip() if auth[:7].lower() == "bearer " else token
    # Encode both sides: hmac.compare_digest raises TypeError on a non-ASCII
    # str, so a non-ASCII STATS_TOKEN would 500 the endpoint instead of gating.
    if not expected or not hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(404)  # 404, not 403 — don't confirm the endpoint exists
    store: Store = app.state.store
    since = time.time() - 30 * 86400
    return HTMLResponse(
        pages.stats_page(
            total=store.hit_count(),
            total_30d=store.hit_count(since),
            referrers=store.top_referrers(since),
            recent=store.recent_hits(50),
        ),
        # No Cache-Control here: the security_headers middleware sets
        # "private, no-store" on every non-public path, /stats included. A
        # weaker header set here would just be overwritten and mislead.
    )


_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
_FONT_PATH = os.path.join(_ASSETS_DIR, "fonts", "LeagueSpartan-VF.ttf")
_DEMO_GIF_PATH = os.path.join(_ASSETS_DIR, "demo.gif")


@app.api_route("/assets/fonts/league-spartan.ttf", methods=["GET", "HEAD"])
async def league_spartan_font():
    # The pages' display face, served from the bundled cover font so the site
    # needs no external font CDN. Immutable + long cache; the file never changes.
    return FileResponse(
        _FONT_PATH,
        media_type="font/ttf",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.api_route("/assets/demo.gif", methods=["GET", "HEAD"])
async def demo_gif():
    # The landing-page demo (KOReader: browse → download → read), bundled so the
    # site is self-contained. Long cache; regenerating it would change the file.
    return FileResponse(
        _DEMO_GIF_PATH,
        media_type="image/gif",
        headers={"Cache-Control": "public, max-age=604800"},
    )


_OG_IMAGE_PATH = os.path.join(_ASSETS_DIR, "og.png")


@app.api_route("/assets/og.png", methods=["GET", "HEAD"])
async def og_image():
    # Social preview card referenced by the landing page's og:image tag.
    # Unfurlers (Discord, Slack...) fetch it server-side and cache aggressively.
    return FileResponse(
        _OG_IMAGE_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "self_host_connectors": list(_connectors.keys()),
        "signup": "free" if config.allow_free_signup() else "paid",
    }


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    # Liveness + shallow readiness for container/orchestrator probes: the app can
    # serve requests once its store was built during startup. Cheap, unauthenticated,
    # and leaks nothing (no connector names or config).
    if getattr(app.state, "store", None) is None:
        raise HTTPException(503, "starting up")
    return {"status": "ok"}


@app.get("/version")
async def version():
    return {"version": __version__}


async def _check_payment(session_id: str | None) -> str | None:
    """Validate the payment gate. Returns an error message, or None if OK."""
    if config.allow_free_signup():
        return None
    stripe_key = config.get_stripe_secret_key()
    if not stripe_key:
        return "Signups are not open yet."
    if not session_id:
        return "Missing payment reference. Please use the link from the checkout page."
    if app.state.store.stripe_ref_used(session_id):
        return "This payment has already been used to create a catalog."
    if not await verify_checkout_session(session_id, stripe_key):
        return "Could not verify payment. If you were charged, contact us."
    return None


@app.get("/start", response_class=HTMLResponse)
async def start_get(session_id: str | None = Query(None)):
    error = await _check_payment(session_id)
    if error:
        return HTMLResponse(pages.start_form(None, error), status_code=403)
    return pages.start_form(session_id)


@app.post("/start", response_class=HTMLResponse)
async def start_post(
    readwise_token: str = Form(...),
    session_id: str | None = Form(None),
):
    error = await _check_payment(session_id)
    if error:
        return HTMLResponse(pages.start_form(None, error), status_code=403)

    readwise_token = readwise_token.strip()
    if not readwise_token or not await readwise.validate_token(readwise_token):
        return HTMLResponse(
            pages.start_form(session_id, "That token was rejected by Readwise. Double-check it and try again."),
            status_code=400,
        )

    try:
        secret = app.state.store.create_user(readwise_token, stripe_ref=session_id)
    except ValueError:
        # The UNIQUE(stripe_ref) insert is the real gate; the stripe_ref_used()
        # check above is only a friendlier early exit, and two concurrent
        # requests carrying the same cs_… can both pass it. Losing that race is
        # a duplicate submission, not a server error.
        return HTMLResponse(
            pages.start_form(None, "This payment has already been used to create a catalog."),
            status_code=403,
        )
    catalog_url = f"{config.get_base_url()}/{secret}/"
    return pages.success(catalog_url, secret, _csrf_token(secret))


# ------------------------------------------- single-user self-host mode


async def _connector_folder_feed(name: str, c: Connector) -> Response:
    folders = await c.list_folders()
    return Response(
        content=opds.folder_catalog(
            f"urn:later-ink:{name}",
            c.description,
            folders,
            base=f"/opds/{name}",
            start_href="/opds/",
            search_href=f"/opds/{name}/search.xml",
        ),
        media_type=NAV_MEDIA,
    )


@app.api_route("/opds/", methods=["GET", "HEAD"])
async def opds_root():
    # With a single connector (the usual self-host case), skip the redundant
    # connector-selection level and present its folders directly — matching the
    # already-flat multi-tenant layout. Only offer a chooser when there's a
    # genuine choice between connectors.
    if len(_connectors) == 1:
        name, c = next(iter(_connectors.items()))
        return await _connector_folder_feed(name, c)
    entries = [(name, c.description) for name, c in _connectors.items()]
    return Response(content=opds.root_catalog(entries), media_type=NAV_MEDIA)


@app.api_route("/opds/{connector}/", methods=["GET", "HEAD"])
async def opds_connector(connector: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _connector_folder_feed(connector, c)


@app.api_route("/opds/{connector}/search.xml", methods=["GET", "HEAD"])
async def opds_search_description(connector: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return Response(
        content=opds.search_description(f"/opds/{connector}/search?q={{searchTerms}}"),
        media_type=OPENSEARCH_MEDIA,
    )


@app.api_route("/opds/{connector}/search", methods=["GET", "HEAD"])
async def opds_search(connector: str, q: str = Query(""), cursor: str | None = Query(None)):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _search_response(
        c,
        q,
        cursor,
        feed_id=f"urn:later-ink:{connector}:search",
        self_href=f"/opds/{connector}/search?q={quote(q)}",
        epub_base=f"/opds/{connector}/articles",
        start_href="/opds/",
    )


@app.get("/opds/{connector}/articles/{article_id}.epub")
async def opds_epub(connector: str, article_id: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _epub_response(c, article_id)


@app.api_route("/opds/{connector}/{folder_id}/", methods=["GET", "HEAD"])
async def opds_folder(connector: str, folder_id: str, cursor: str | None = Query(None)):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _folder_response(
        c,
        folder_id,
        cursor,
        feed_id=f"urn:later-ink:{connector}:{folder_id}",
        self_href=f"/opds/{connector}/{folder_id}/",
        epub_base=f"/opds/{connector}/articles",
        start_href="/opds/",
    )


# ------------------------------------------------- multi-tenant mode


@app.post("/{secret}/regenerate", response_class=HTMLResponse)
async def tenant_regenerate(secret: str, request: Request, csrf: str | None = Form(None)):
    _resolve_secret(secret, request)
    _require_csrf(secret, csrf)
    new_secret = app.state.store.regenerate_secret(secret)
    if new_secret is None:
        raise HTTPException(404)
    catalog_url = f"{config.get_base_url()}/{new_secret}/"
    return pages.success(catalog_url, new_secret, _csrf_token(new_secret))


@app.post("/{secret}/delete", response_class=HTMLResponse)
async def tenant_delete(secret: str, request: Request, csrf: str | None = Form(None)):
    token = _resolve_secret(secret, request)
    _require_csrf(secret, csrf)
    app.state.store.delete_user(secret)
    conn = _tenant_connectors.pop(token, None)
    if conn is not None:
        await conn.close()
    return pages.deleted()


@app.api_route("/{secret}/", methods=["GET", "HEAD"])
async def tenant_root(secret: str, request: Request):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    folders = await c.list_folders()
    return Response(
        content=opds.folder_catalog(
            _feed_id(secret),
            "Later.Ink",
            folders,
            base=f"/{secret}",
            start_href=f"/{secret}/",
            search_href=f"/{secret}/search.xml",
        ),
        media_type=NAV_MEDIA,
    )


@app.get("/{secret}/articles/{article_id}.epub")
async def tenant_epub(secret: str, article_id: str, request: Request):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    return await _epub_response(c, article_id)


@app.api_route("/{secret}/search.xml", methods=["GET", "HEAD"])
async def tenant_search_description(secret: str, request: Request):
    _resolve_secret(secret, request)
    return Response(
        content=opds.search_description(f"/{secret}/search?q={{searchTerms}}"),
        media_type=OPENSEARCH_MEDIA,
    )


@app.api_route("/{secret}/search", methods=["GET", "HEAD"])
async def tenant_search(
    secret: str,
    request: Request,
    q: str = Query(""),
    cursor: str | None = Query(None),
):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    return await _search_response(
        c,
        q,
        cursor,
        feed_id=f"{_feed_id(secret)}:search",
        self_href=f"/{secret}/search?q={quote(q)}",
        epub_base=f"/{secret}/articles",
        start_href=f"/{secret}/",
    )


@app.api_route("/{secret}/{folder_id}/", methods=["GET", "HEAD"])
async def tenant_folder(
    secret: str,
    folder_id: str,
    request: Request,
    cursor: str | None = Query(None),
):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    return await _folder_response(
        c,
        folder_id,
        cursor,
        feed_id=f"{_feed_id(secret)}:{folder_id}",
        self_href=f"/{secret}/{folder_id}/",
        epub_base=f"/{secret}/articles",
        start_href=f"/{secret}/",
    )


# ------------------------------------------------------- shared logic


async def _folder_response(
    c: Connector,
    folder_id: str,
    cursor: str | None,
    feed_id: str,
    self_href: str,
    epub_base: str,
    start_href: str,
) -> Response:
    folders = await c.list_folders()
    folder = next((f for f in folders if f.id == folder_id), None)
    if not folder:
        raise HTTPException(404, f"Folder '{folder_id}' not found")

    articles, next_cursor = await c.list_articles(folder_id, cursor)
    return Response(
        content=opds.article_feed(
            feed_id,
            folder.title,
            articles,
            self_href=self_href,
            epub_base=epub_base,
            start_href=start_href,
            next_cursor=next_cursor,
        ),
        media_type=ACQ_MEDIA,
    )


async def _search_response(
    c: Connector,
    query: str,
    cursor: str | None,
    feed_id: str,
    self_href: str,
    epub_base: str,
    start_href: str,
) -> Response:
    articles, next_cursor = await c.search(query, cursor)
    return Response(
        content=opds.article_feed(
            feed_id,
            f"Search: {query}" if query.strip() else "Search",
            articles,
            self_href=self_href,
            epub_base=epub_base,
            start_href=start_href,
            next_cursor=next_cursor,
        ),
        media_type=ACQ_MEDIA,
    )


async def _epub_response(c: Connector, article_id: str) -> Response:
    article, html_content = await c.get_article_html(article_id)
    epub_bytes = await build_epub(
        title=article.title,
        author=article.author,
        html_content=html_content,
        source_url=article.url,
        identifier=article.id,
        language=article.language or "en",
        preserve_styles=(article.category == "epub"),
        image_url=article.image_url,
        raw_cover=(article.category == "epub"),
    )
    safe_title = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in article.title)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.epub"'},
    )
