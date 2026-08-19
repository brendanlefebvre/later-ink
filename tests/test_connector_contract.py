"""What every connector must do, run against every connector.

Read this file as the contract. Each connector registers how to build itself
and what its own API returns for a handful of situations; the assertions below
are shared, because the situations are shared even though the bytes are not —
a missing article is an empty results list on Readwise and a 404 on Wallabag.

Adding a connector means adding a ConnectorSpec, not another test file.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest

from later_ink.connectors.base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
)
from later_ink.connectors.readwise import ReadwiseConnector
from later_ink.connectors.wallabag import WallabagConnector

FOLDER_ID_READWISE = "later"
FOLDER_ID_WALLABAG = "unread"
ARTICLE_ID = "42"


@dataclass
class ConnectorSpec:
    """Everything the contract needs to exercise one connector.

    `handlers` maps a scenario name to a transport handler, because the same
    situation looks different on each API. Keeping it on the spec — rather than
    in a lookup keyed by connector name — is what makes adding a connector a
    single registry entry with nothing else to remember.
    """

    label: str                                  # test id only
    cls: type[Connector]                        # for the class-level assertions
    build: Callable[[Callable], Connector]      # handler -> connector
    handlers: Callable[[str], Callable]         # scenario -> handler
    folder_id: str
    article_id: str


def _readwise_handler(scenario: str) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "error_500":
            return httpx.Response(500)
        if scenario == "unauthorized":
            return httpx.Response(401)
        if scenario == "non_json":
            return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
        if scenario == "missing":
            return httpx.Response(200, json={"results": [], "nextPageCursor": None})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": ARTICLE_ID,
                        "title": "An article",
                        "category": "article",
                        "saved_at": "2025-01-02T03:04:05+02:00",
                        "html_content": "<p>body</p>",
                    }
                ],
                "nextPageCursor": None,
            },
        )

    return handler


def _wallabag_handler(scenario: str) -> Callable:
    entry = {
        "id": int(ARTICLE_ID),
        "title": "An article",
        "url": "https://example.com/a",
        "created_at": "2025-01-02T03:04:05+02:00",
        "content": "<p>body</p>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/token":
            return httpx.Response(
                200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600}
            )
        if scenario == "error_500":
            return httpx.Response(500)
        if scenario == "unauthorized":
            # Wallabag re-authenticates once on a 401 before giving up, so this
            # must stay 401 on the retry too.
            return httpx.Response(401)
        if scenario == "non_json":
            return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
        if scenario == "missing":
            return httpx.Response(404)
        if request.url.path.startswith("/api/entries/"):
            return httpx.Response(200, json=entry)
        return httpx.Response(200, json={"_embedded": {"items": [entry]}, "page": 1, "pages": 1})

    return handler


SPECS = [
    ConnectorSpec(
        label="readwise",
        cls=ReadwiseConnector,
        build=lambda handler: ReadwiseConnector(
            "tok",
            client=httpx.AsyncClient(
                base_url="https://readwise.test", transport=httpx.MockTransport(handler)
            ),
        ),
        handlers=_readwise_handler,
        folder_id=FOLDER_ID_READWISE,
        article_id=ARTICLE_ID,
    ),
    ConnectorSpec(
        label="wallabag",
        cls=WallabagConnector,
        build=lambda handler: WallabagConnector(
            url="https://wb.test",
            client_id="cid",
            client_secret="csec",
            username="user",
            password="pass",
            client=httpx.AsyncClient(
                base_url="https://wb.test", transport=httpx.MockTransport(handler)
            ),
        ),
        handlers=_wallabag_handler,
        folder_id=FOLDER_ID_WALLABAG,
        article_id=ARTICLE_ID,
    ),
]


def _run(spec: ConnectorSpec, scenario: str, call):
    async def go():
        conn = spec.build(spec.handlers(scenario))
        try:
            return await call(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


@pytest.fixture(params=SPECS, ids=lambda s: s.label)
def spec(request):
    return request.param


def test_name_is_a_non_empty_string(spec):
    # Connector.name is part of the EPUB cache key, so a blank or duplicated
    # name would cross-contaminate cached books between connectors.
    assert isinstance(spec.cls.name, str) and spec.cls.name


def test_connector_names_are_unique():
    # Asserted on the connectors themselves, not on the registry labels — two
    # connectors could be registered under different labels and still ship the
    # same `name`, which is the collision that matters.
    names = [s.cls.name for s in SPECS]
    assert len(names) == len(set(names))


def test_list_folders_returns_usable_folders(spec):
    folders = _run(spec, "ok", lambda c: c.list_folders())
    assert folders
    for f in folders:
        assert isinstance(f, Folder)
        assert isinstance(f.id, str) and f.id
        assert isinstance(f.title, str) and f.title


def test_list_articles_returns_articles_and_a_cursor(spec):
    articles, cursor = _run(spec, "ok", lambda c: c.list_articles(spec.folder_id))
    assert articles
    assert cursor is None or isinstance(cursor, str)
    for a in articles:
        assert isinstance(a, Article)
        assert isinstance(a.id, str) and a.id


def test_content_date_is_naive_utc(spec):
    # The determinism guard. dcterms:modified comes from content_date, and
    # ebooklib formats it with a literal Z and no conversion, so an aware value
    # would be written as UTC while carrying local wall-clock time — and the
    # bytes would drift if that offset ever moved. Both handlers above feed a
    # +02:00 timestamp precisely so a connector that forgets base.parse_dt
    # fails here.
    articles, _ = _run(spec, "ok", lambda c: c.list_articles(spec.folder_id))
    for a in articles:
        assert a.content_date is None or a.content_date.tzinfo is None


def test_get_article_html_returns_an_article_and_html(spec):
    article, html = _run(spec, "ok", lambda c: c.get_article_html(spec.article_id))
    assert isinstance(article, Article)
    assert isinstance(html, str) and html.strip()


def test_a_missing_article_raises_article_unavailable(spec):
    # Not UpstreamError: the two are handled differently. ArticleUnavailable
    # carries an explanation the reader sees and a 404/422; UpstreamError means
    # the service itself is broken.
    with pytest.raises(ArticleUnavailable):
        _run(spec, "missing", lambda c: c.get_article_html(spec.article_id))


@pytest.mark.parametrize("scenario", ["error_500", "unauthorized", "non_json"])
def test_upstream_problems_raise_upstream_error(spec, scenario):
    with pytest.raises(UpstreamError):
        _run(spec, scenario, lambda c: c.list_articles(spec.folder_id))


def test_an_unreachable_host_raises_upstream_error(spec):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async def go():
        conn = spec.build(handler)
        try:
            with pytest.raises(UpstreamError):
                await conn.list_articles(spec.folder_id)
        finally:
            await conn.close()

    asyncio.run(go())


def test_close_is_safe_to_call_twice(spec):
    async def go():
        conn = spec.build(spec.handlers("ok"))
        await conn.close()
        await conn.close()

    asyncio.run(go())
