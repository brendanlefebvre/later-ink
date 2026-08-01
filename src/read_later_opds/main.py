import hashlib
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from . import config, opds, pages
from .connectors import readwise
from .connectors.base import Connector
from .connectors.readwise import ReadwiseConnector
from .epub import build_epub
from .payments import verify_checkout_session
from .ratelimit import MissLimiter
from .store import RESERVED_PATHS, Store

NAV_MEDIA = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_MEDIA = "application/atom+xml;profile=opds-catalog;kind=acquisition"

MAX_TENANT_CONNECTORS = 200

# Single-user (self-host) connectors, keyed by connector name
_connectors: dict[str, Connector] = {}
# Multi-tenant connector cache, keyed by Readwise token
_tenant_connectors: OrderedDict[str, ReadwiseConnector] = OrderedDict()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store(config.get_database_path())
    app.state.limiter = MissLimiter(limit=20, window=3600.0)
    token = config.get_readwise_token()
    if token:
        _connectors["readwise"] = ReadwiseConnector(token)
    yield
    for c in _connectors.values():
        if hasattr(c, "close"):
            await c.close()
    _connectors.clear()
    for c in _tenant_connectors.values():
        await c.close()
    _tenant_connectors.clear()


app = FastAPI(
    title="read-later-opds",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


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
    fwd = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _resolve_secret(secret: str, request: Request) -> str:
    """Map a URL secret to a Readwise token, rate-limiting unknown-secret probes."""
    if secret in RESERVED_PATHS:
        raise HTTPException(404)
    limiter: MissLimiter = app.state.limiter
    ip = _client_ip(request)
    if limiter.blocked(ip):
        raise HTTPException(429, "Too many attempts; try again later")
    token = app.state.store.get_token(secret)
    if token is None:
        limiter.record_miss(ip)
        raise HTTPException(404, "Unknown catalog")
    return token


def _feed_id(secret: str) -> str:
    # Stable per-user feed id that doesn't echo the secret itself
    return "urn:read-later-opds:u:" + hashlib.sha1(secret.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def landing():
    return pages.landing(config.get_stripe_payment_link(), config.allow_free_signup())


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "self_host_connectors": list(_connectors.keys()),
        "signup": "free" if config.allow_free_signup() else "paid",
    }


async def _check_payment(session_id: str | None) -> str | None:
    """Validate the payment gate. Returns an error message, or None if OK."""
    if config.allow_free_signup():
        return None
    stripe_key = config.get_stripe_secret_key()
    if not stripe_key:
        return "Signups are not open yet."
    if not session_id:
        return "Missing payment reference — please use the link from the checkout page."
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
            pages.start_form(session_id, "That token was rejected by Readwise — double-check it and try again."),
            status_code=400,
        )

    secret = app.state.store.create_user(readwise_token, stripe_ref=session_id)
    catalog_url = f"{config.get_base_url()}/{secret}/"
    return pages.success(catalog_url, secret)


# ------------------------------------------- single-user self-host mode


@app.get("/opds/")
async def opds_root():
    entries = [(name, c.description) for name, c in _connectors.items()]
    return Response(content=opds.root_catalog(entries), media_type=NAV_MEDIA)


@app.get("/opds/{connector}/")
async def opds_connector(connector: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    folders = await c.list_folders()
    return Response(
        content=opds.folder_catalog(
            f"urn:read-later-opds:{connector}",
            c.description,
            folders,
            base=f"/opds/{connector}",
            start_href="/opds/",
        ),
        media_type=NAV_MEDIA,
    )


@app.get("/opds/{connector}/articles/{article_id}.epub")
async def opds_epub(connector: str, article_id: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _epub_response(c, article_id)


@app.get("/opds/{connector}/{folder_id}/")
async def opds_folder(connector: str, folder_id: str, cursor: str | None = Query(None)):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")
    return await _folder_response(
        c,
        folder_id,
        cursor,
        feed_id=f"urn:read-later-opds:{connector}:{folder_id}",
        self_href=f"/opds/{connector}/{folder_id}/",
        epub_base=f"/opds/{connector}/articles",
        start_href="/opds/",
    )


# ------------------------------------------------- multi-tenant mode


@app.post("/{secret}/regenerate", response_class=HTMLResponse)
async def tenant_regenerate(secret: str, request: Request):
    _resolve_secret(secret, request)
    new_secret = app.state.store.regenerate_secret(secret)
    if new_secret is None:
        raise HTTPException(404)
    catalog_url = f"{config.get_base_url()}/{new_secret}/"
    return pages.success(catalog_url, new_secret)


@app.post("/{secret}/delete", response_class=HTMLResponse)
async def tenant_delete(secret: str, request: Request):
    token = _resolve_secret(secret, request)
    app.state.store.delete_user(secret)
    conn = _tenant_connectors.pop(token, None)
    if conn is not None:
        await conn.close()
    return pages.deleted()


@app.get("/{secret}/")
async def tenant_root(secret: str, request: Request):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    folders = await c.list_folders()
    return Response(
        content=opds.folder_catalog(
            _feed_id(secret),
            "Read Later",
            folders,
            base=f"/{secret}",
            start_href=f"/{secret}/",
        ),
        media_type=NAV_MEDIA,
    )


@app.get("/{secret}/articles/{article_id}.epub")
async def tenant_epub(secret: str, article_id: str, request: Request):
    token = _resolve_secret(secret, request)
    c = await _tenant_connector(token)
    return await _epub_response(c, article_id)


@app.get("/{secret}/{folder_id}/")
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


async def _epub_response(c: Connector, article_id: str) -> Response:
    article, html_content = await c.get_article_html(article_id)
    epub_bytes = build_epub(
        title=article.title,
        author=article.author,
        html_content=html_content,
        source_url=article.url,
    )
    safe_title = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in article.title)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.epub"'},
    )
