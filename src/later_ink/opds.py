from datetime import UTC, datetime
from urllib.parse import quote

from lxml import etree

from .connectors.base import Article, Folder

ATOM_NS = "http://www.w3.org/2005/Atom"
OPDS_NS = "http://opds-spec.org/2010/catalog"
DC_NS = "http://purl.org/dc/terms/"

NSMAP = {None: ATOM_NS, "opds": OPDS_NS, "dc": DC_NS}

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB_TYPE = "application/epub+zip"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _abs(origin: str, path: str) -> str:
    """Absolute URL for a root-relative feed path.

    Every href the catalog hands out is absolute rather than root-relative.
    Resolving `/opds/x/` against the catalog URL is the client's job, and
    Moon+ Reader gets it wrong: it treats the leading slash as the start of a
    host, so `/opds/readwise/` becomes `http://opds/readwise/` and nothing
    below the top level opens. KOReader, Thorium and the rest resolve either
    form, and the big OPDS servers (Komga, Kavita, calibre-web) all emit
    absolute links for the same reason. An already-absolute href, or anything
    that is not a path, passes through untouched.
    """
    return f"{origin}{path}" if path.startswith("/") else path


def _add_text(parent: etree._Element, tag: str, text: str) -> etree._Element:
    el = etree.SubElement(parent, tag)
    el.text = text
    return el


def _add_link(
    parent: etree._Element,
    rel: str,
    href: str,
    media_type: str,
) -> etree._Element:
    link = etree.SubElement(parent, "link")
    link.set("rel", rel)
    link.set("href", href)
    link.set("type", media_type)
    return link


def _make_feed(
    feed_id: str,
    title: str,
    self_href: str,
    start_href: str,
    self_type: str = NAV_TYPE,
    origin: str = "",
) -> etree._Element:
    feed = etree.Element(f"{{{ATOM_NS}}}feed", nsmap=NSMAP)
    _add_text(feed, "id", feed_id)
    _add_text(feed, "title", title)
    _add_text(feed, "updated", _now_iso())
    _add_link(feed, "self", _abs(origin, self_href), self_type)
    _add_link(feed, "start", _abs(origin, start_href), NAV_TYPE)
    return feed


def _serialize(feed: etree._Element) -> bytes:
    return etree.tostring(feed, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def root_catalog(
    connectors: list[tuple[str, str]], base: str = "/opds", origin: str = ""
) -> bytes:
    feed = _make_feed("urn:later-ink:root", "Later.Ink", f"{base}/", f"{base}/", origin=origin)

    for name, description in connectors:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"urn:later-ink:{name}")
        _add_text(entry, "title", description)
        _add_text(entry, "updated", _now_iso())
        content = etree.SubElement(entry, "content")
        content.set("type", "text")
        content.text = f"Articles from {description}"
        _add_link(entry, "subsection", _abs(origin, f"{base}/{name}/"), NAV_TYPE)

    return _serialize(feed)


def search_description(search_template: str, origin: str = "") -> bytes:
    """OpenSearch description document.

    `search_template` must contain the OpenSearch {searchTerms} placeholder;
    the client substitutes the user's query and requests the resulting URL.
    """
    search_template = _abs(origin, search_template)
    root = etree.Element(f"{{{OPENSEARCH_NS}}}OpenSearchDescription", nsmap={None: OPENSEARCH_NS})
    _add_text(root, "ShortName", "Later.Ink")
    _add_text(root, "Description", "Search your saved articles")
    url = etree.SubElement(root, "Url")
    url.set("type", ACQ_TYPE)
    url.set("template", search_template)
    return _serialize(root)


def folder_catalog(
    feed_id: str,
    title: str,
    folders: list[Folder],
    base: str,
    start_href: str,
    search_href: str | None = None,
    origin: str = "",
) -> bytes:
    feed = _make_feed(feed_id, title, f"{base}/", start_href, origin=origin)
    if search_href:
        _add_link(feed, "search", _abs(origin, search_href), OPENSEARCH_TYPE)

    for folder in folders:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"{feed_id}:{folder.id}")
        _add_text(entry, "title", folder.title)
        _add_text(entry, "updated", _now_iso())
        if folder.description:
            content = etree.SubElement(entry, "content")
            content.set("type", "text")
            content.text = folder.description
        _add_link(entry, "subsection", _abs(origin, f"{base}/{folder.id}/"), ACQ_TYPE)

    return _serialize(feed)


def article_feed(
    feed_id: str,
    title: str,
    articles: list[Article],
    self_href: str,
    epub_base: str,
    start_href: str,
    next_cursor: str | None = None,
    origin: str = "",
) -> bytes:
    feed = _make_feed(feed_id, title, self_href, start_href, ACQ_TYPE, origin=origin)

    if next_cursor:
        sep = "&" if "?" in self_href else "?"
        # Encode the cursor as a single value so its contents can't inject
        # additional query parameters into the next link.
        next_href = f"{self_href}{sep}cursor={quote(next_cursor, safe='')}"
        _add_link(feed, "next", _abs(origin, next_href), ACQ_TYPE)

    for article in articles:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"{feed_id}:article:{article.id}")
        _add_text(entry, "title", article.title)
        _add_text(entry, "updated", article.updated.strftime("%Y-%m-%dT%H:%M:%SZ"))

        if article.author:
            author_el = etree.SubElement(entry, "author")
            _add_text(author_el, "name", article.author)

        if article.summary:
            summary = etree.SubElement(entry, "summary")
            summary.set("type", "text")
            summary.text = article.summary[:500]

        _add_link(
            entry,
            "http://opds-spec.org/acquisition",
            _abs(origin, f"{epub_base}/{article.id}.epub"),
            EPUB_TYPE,
        )

    return _serialize(feed)
