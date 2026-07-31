from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from . import opds
from .config import get_readwise_token
from .connectors.base import Connector
from .connectors.readwise import ReadwiseConnector
from .epub import build_epub

app = FastAPI(title="read-later-opds", version="0.1.0")

_connectors: dict[str, Connector] = {}


@app.on_event("startup")
async def startup():
    token = get_readwise_token()
    if token:
        _connectors["readwise"] = ReadwiseConnector(token)


@app.on_event("shutdown")
async def shutdown():
    for c in _connectors.values():
        if hasattr(c, "close"):
            await c.close()


@app.get("/health")
async def health():
    return {"status": "ok", "connectors": list(_connectors.keys())}


@app.get("/opds/")
async def opds_root():
    entries = [(name, c.description) for name, c in _connectors.items()]
    return Response(
        content=opds.root_catalog(entries),
        media_type="application/atom+xml;profile=opds-catalog;kind=navigation",
    )


@app.get("/opds/{connector}/")
async def opds_connector(connector: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")

    folders = await c.list_folders()
    return Response(
        content=opds.connector_catalog(connector, c.description, folders),
        media_type="application/atom+xml;profile=opds-catalog;kind=navigation",
    )


@app.get("/opds/{connector}/{folder_id}/")
async def opds_folder(
    connector: str,
    folder_id: str,
    cursor: str | None = Query(None),
):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")

    folders = await c.list_folders()
    folder = next((f for f in folders if f.id == folder_id), None)
    if not folder:
        raise HTTPException(404, f"Folder '{folder_id}' not found")

    articles, next_cursor = await c.list_articles(folder_id, cursor)
    return Response(
        content=opds.article_feed(
            connector, folder_id, folder.title, articles, next_cursor
        ),
        media_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
    )


@app.get("/opds/{connector}/articles/{article_id}.epub")
async def opds_epub(connector: str, article_id: str):
    c = _connectors.get(connector)
    if not c:
        raise HTTPException(404, f"Connector '{connector}' not found")

    article, html_content = await c.get_article_html(article_id)
    epub_bytes = build_epub(
        title=article.title,
        author=article.author,
        html_content=html_content,
        source_url=article.url,
    )

    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in article.title)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.epub"'},
    )
