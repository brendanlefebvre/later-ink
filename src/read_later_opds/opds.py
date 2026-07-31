from datetime import datetime, timezone

from lxml import etree

from .connectors.base import Article, Folder

ATOM_NS = "http://www.w3.org/2005/Atom"
OPDS_NS = "http://opds-spec.org/2010/catalog"
DC_NS = "http://purl.org/dc/terms/"

NSMAP = {None: ATOM_NS, "opds": OPDS_NS, "dc": DC_NS}

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB_TYPE = "application/epub+zip"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    self_type: str = NAV_TYPE,
) -> etree._Element:
    feed = etree.Element(f"{{{ATOM_NS}}}feed", nsmap=NSMAP)
    _add_text(feed, "id", feed_id)
    _add_text(feed, "title", title)
    _add_text(feed, "updated", _now_iso())
    _add_link(feed, "self", self_href, self_type)
    _add_link(feed, "start", "/opds/", NAV_TYPE)
    return feed


def _serialize(feed: etree._Element) -> bytes:
    return etree.tostring(feed, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def root_catalog(connectors: list[tuple[str, str]]) -> bytes:
    feed = _make_feed("urn:read-later-opds:root", "Read Later", "/opds/")

    for name, description in connectors:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"urn:read-later-opds:{name}")
        _add_text(entry, "title", description)
        _add_text(entry, "updated", _now_iso())
        content = etree.SubElement(entry, "content")
        content.set("type", "text")
        content.text = f"Articles from {description}"
        _add_link(entry, "subsection", f"/opds/{name}/", NAV_TYPE)

    return _serialize(feed)


def connector_catalog(
    connector_name: str,
    connector_title: str,
    folders: list[Folder],
) -> bytes:
    feed = _make_feed(
        f"urn:read-later-opds:{connector_name}",
        connector_title,
        f"/opds/{connector_name}/",
    )

    for folder in folders:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"urn:read-later-opds:{connector_name}:{folder.id}")
        _add_text(entry, "title", folder.title)
        _add_text(entry, "updated", _now_iso())
        if folder.description:
            content = etree.SubElement(entry, "content")
            content.set("type", "text")
            content.text = folder.description
        _add_link(
            entry,
            "subsection",
            f"/opds/{connector_name}/{folder.id}/",
            ACQ_TYPE,
        )

    return _serialize(feed)


def article_feed(
    connector_name: str,
    folder_id: str,
    folder_title: str,
    articles: list[Article],
    next_cursor: str | None = None,
) -> bytes:
    self_href = f"/opds/{connector_name}/{folder_id}/"
    feed = _make_feed(
        f"urn:read-later-opds:{connector_name}:{folder_id}",
        f"{folder_title}",
        self_href,
        ACQ_TYPE,
    )

    if next_cursor:
        _add_link(
            feed,
            "next",
            f"/opds/{connector_name}/{folder_id}/?cursor={next_cursor}",
            ACQ_TYPE,
        )

    for article in articles:
        entry = etree.SubElement(feed, "entry")
        _add_text(entry, "id", f"urn:read-later-opds:{connector_name}:article:{article.id}")
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
            f"/opds/{connector_name}/articles/{article.id}.epub",
            EPUB_TYPE,
        )

    return _serialize(feed)
