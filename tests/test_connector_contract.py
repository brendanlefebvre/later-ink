"""What every connector must do, run against every connector.

Read this file as the contract. Each connector registers how to build itself
and what its own API returns for a handful of situations; the assertions below
are shared, because the situations are shared even though the bytes are not —
a missing article is an empty results list on Readwise and a 404 on Wallabag.

Adding a connector means adding a ConnectorSpec, not another test file.
"""

import asyncio
import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
import pytest

import later_ink.connectors as connectors_pkg
from later_ink.connectors.base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
    retry_after_seconds,
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

    label: str                                          # test id only
    cls: type[Connector]                                # for the class-level assertions
    # handler -> (connector, its http client). The client comes back alongside
    # the connector, rather than being reconstructed from it, so the close()
    # contract test can check the one client it actually asked the connector
    # to release.
    build: Callable[[Callable], tuple[Connector, httpx.AsyncClient]]
    handlers: Callable[[str], Callable]                 # scenario -> handler
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
        if scenario == "unreachable":
            # Readwise has no pre-flight auth call, so this is already the data
            # request — nothing else needs to succeed first.
            raise httpx.ConnectError("no route to host")
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
            # Must keep succeeding even under "unreachable": Wallabag's _get
            # calls _ensure_token() before the data request, so failing this
            # too would exercise the auth path's error mapping instead of the
            # data request's — not what the scenario is testing.
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
        if scenario == "unreachable":
            raise httpx.ConnectError("no route to host")
        if request.url.path.startswith("/api/entries/"):
            return httpx.Response(200, json=entry)
        return httpx.Response(200, json={"_embedded": {"items": [entry]}, "page": 1, "pages": 1})

    return handler


def _build_readwise(handler: Callable) -> tuple[Connector, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://readwise.test", transport=httpx.MockTransport(handler))
    return ReadwiseConnector("tok", client=client), client


def _build_wallabag(handler: Callable) -> tuple[Connector, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://wb.test", transport=httpx.MockTransport(handler))
    connector = WallabagConnector(
        url="https://wb.test",
        client_id="cid",
        client_secret="csec",
        username="user",
        password="pass",
        client=client,
    )
    return connector, client


SPECS = [
    ConnectorSpec(
        label="readwise",
        cls=ReadwiseConnector,
        build=_build_readwise,
        handlers=_readwise_handler,
        folder_id=FOLDER_ID_READWISE,
        article_id=ARTICLE_ID,
    ),
    ConnectorSpec(
        label="wallabag",
        cls=WallabagConnector,
        build=_build_wallabag,
        handlers=_wallabag_handler,
        folder_id=FOLDER_ID_WALLABAG,
        article_id=ARTICLE_ID,
    ),
]


def _run(spec: ConnectorSpec, scenario: str, call):
    async def go():
        conn, _client = spec.build(spec.handlers(scenario))
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


# The determinism guard. dcterms:modified comes from content_date, and
# ebooklib formats it with a literal Z and no conversion, so an aware value
# would be written as UTC while carrying local wall-clock time — and the bytes
# would drift if that offset ever moved. Both handlers above feed a +02:00
# timestamp precisely so a connector that forgets base.parse_dt fails here.
# CONTENT_DATE_UTC is that timestamp converted to UTC, not just stripped of
# tzinfo: datetime.fromisoformat(...).replace(tzinfo=None) also satisfies
# "tzinfo is None" while keeping the wrong (local) wall-clock value, so the
# assertion below checks the value, not the empty tzinfo slot.
CONTENT_DATE_UTC = datetime(2025, 1, 2, 1, 4, 5)


def test_content_date_is_naive_utc(spec):
    articles, _ = _run(spec, "ok", lambda c: c.list_articles(spec.folder_id))
    assert articles
    assert [a.content_date for a in articles] == [CONTENT_DATE_UTC]


def test_the_downloaded_article_content_date_is_naive_utc(spec):
    # The Article that reaches build_epub — and therefore dcterms:modified —
    # comes from get_article_html, not list_articles (main.py's _epub_response
    # calls get_article_html). Checking only list_articles lets that path drift
    # unnoticed; both connectors currently share one _article_from_* helper
    # across both methods, but a future connector need not.
    article, _ = _run(spec, "ok", lambda c: c.get_article_html(spec.article_id))
    assert article.content_date == CONTENT_DATE_UTC


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
    # Uses the connector's own handler (via the "unreachable" scenario) rather
    # than a bare ConnectError-for-everything handler: Wallabag's _get calls
    # _ensure_token() before the data GET, so failing every request only ever
    # exercised _fetch_token's error mapping and never reached list_articles'.
    with pytest.raises(UpstreamError):
        _run(spec, "unreachable", lambda c: c.list_articles(spec.folder_id))


def test_close_releases_the_http_client_and_is_safe_to_call_twice(spec):
    # A close() that does nothing would satisfy "raises nothing on a second
    # call" too, so the real assertion is that the first call actually
    # released the client — not just that a second call is harmless.
    async def go():
        conn, client = spec.build(spec.handlers("ok"))
        await conn.close()
        assert client.is_closed
        await conn.close()

    asyncio.run(go())


def _all_subclasses(cls: type) -> set[type]:
    # Connector.__subclasses__() alone only returns *direct* subclasses, so a
    # connector inheriting through a shared intermediate base (plausible once
    # a third connector arrives) would never show up — the whole point of
    # this test defeated by exactly the shape it should be checking for.
    # Walking recursively is what makes that unregistered connector visible.
    direct = cls.__subclasses__()
    return set(direct).union(*(_all_subclasses(sub) for sub in direct)) if direct else set()


def test_every_shipped_connector_is_registered():
    # A connector added without a ConnectorSpec entry is silently unverified.
    # Connector.__subclasses__() alone would only see classes some earlier
    # import happened to load — passing vacuously when this file runs by
    # itself, and only catching a missing registration by accident of import
    # order elsewhere in the suite. Walking the package's own modules first
    # makes discovery independent of what else has been imported.
    for _, module_name, _ in pkgutil.iter_modules(
        connectors_pkg.__path__, connectors_pkg.__name__ + "."
    ):
        importlib.import_module(module_name)

    # Filtering by defining module (rather than trusting __subclasses__ alone)
    # excludes test doubles like tests/test_views.py's PagedConnector and
    # tests/test_search.py's FakeConnector, which subclass Connector but are
    # not shipped connectors. The isabstract() filter excludes an abstract
    # intermediate base within the package itself — recursion means it's now
    # in scope, but it's a shared base, not a shipped connector, and demanding
    # it have its own ConnectorSpec would be asking it to be instantiable.
    shipped = {
        cls
        for cls in _all_subclasses(Connector)
        if cls.__module__.startswith(connectors_pkg.__name__ + ".") and not inspect.isabstract(cls)
    }
    assert shipped == {s.cls for s in SPECS}


# retry_after_seconds is shared by both connectors' retry loops, not tied to
# either one's API shape, so it belongs here rather than in a per-connector
# file.


def test_retry_after_seconds_passes_through_a_valid_value():
    resp = httpx.Response(429, headers={"Retry-After": "3"})
    assert retry_after_seconds(resp) == 3.0


def test_retry_after_seconds_caps_a_value_above_the_cap():
    resp = httpx.Response(429, headers={"Retry-After": "9999"})
    assert retry_after_seconds(resp) == 15.0


def test_retry_after_seconds_falls_back_to_default_on_a_negative_value():
    # -1 parses fine as a float, so this only fails for the right reason if
    # the helper rejects it after parsing rather than trusting float()'s
    # success.
    resp = httpx.Response(429, headers={"Retry-After": "-1"})
    assert retry_after_seconds(resp) == 2.0


def test_retry_after_seconds_falls_back_to_default_on_nan():
    # NaN also parses fine as a float — and an unguarded NaN reaching
    # asyncio.sleep raises ValueError, escaping as a 500 to the reader.
    resp = httpx.Response(429, headers={"Retry-After": "NaN"})
    assert retry_after_seconds(resp) == 2.0


def test_retry_after_seconds_falls_back_to_default_on_a_non_numeric_value():
    resp = httpx.Response(429, headers={"Retry-After": "soon"})
    assert retry_after_seconds(resp) == 2.0
