import hashlib
import io
import logging
import re
from html import escape

import httpx
from ebooklib import epub
from lxml.html import fromstring, tostring

from . import covers
from .fetch import fetch_bytes

logger = logging.getLogger(__name__)

# One image-heavy download must not blow up over a Kobo's wifi.
MAX_IMAGES = 30
MAX_IMAGE_BYTES_TOTAL = 15 * 1024 * 1024
# Per-image cap as well as the total: without it a single hostile response
# could be streamed to the total limit before anything noticed.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_FETCH_TIMEOUT = 10.0

_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}
_IMAGE_TYPES = frozenset(_EXT_BY_TYPE)

NORMALIZE_CSS = (
    "body { font-family: serif; line-height: 1.6; max-width: 40em; margin: 0 auto; "
    "padding: 1em; } img { max-width: 100%; height: auto; }"
)

# The EPUB3 nav document is an <ol>, so a reader that shows it as a spine page
# renders it numbered. This stylesheet is linked only from the nav document, so
# it makes that on-page table of contents an unnumbered list without touching the
# <ol> markup the reader's own ToC menu relies on.
NAV_CSS = "ol { list-style: none; padding-left: 0; margin-left: 0; }"

# Readwise tags each section of a parsed EPUB with this attribute; it's the most
# reliable split point. Falls back to <section>, then to a single chapter.
_TOC_ATTR = "data-rw-epub-toc"


def _fallback_html(title: str, source_url: str | None) -> str:
    link = (
        f'<p>Read it at the source: <a href="{escape(source_url)}">{escape(source_url)}</a></p>'
        if source_url
        else ""
    )
    return (
        f"<h1>{escape(title)}</h1>"
        f"<p>This item could not be converted for offline reading.</p>{link}"
    )


async def _embed_images(doc, client: httpx.AsyncClient) -> list[epub.EpubItem]:
    """Fetch remote <img> targets and rewrite them to in-book paths.

    Offline is the product's premise; a remote src renders as a broken box on
    an e-reader. Failures leave the original reference in place — a broken
    image beats a failed download.
    """
    items: list[epub.EpubItem] = []
    total_bytes = 0
    for i, img in enumerate(doc.iter("img")):
        src = img.get("src") or ""
        if not src.startswith(("http://", "https://")):
            continue
        if len(items) >= MAX_IMAGES or total_bytes >= MAX_IMAGE_BYTES_TOTAL:
            break
        # src is chosen by whoever wrote the document — see fetch.py for why
        # this can't be a plain client.get.
        got = await fetch_bytes(
            client,
            src,
            timeout=IMAGE_FETCH_TIMEOUT,
            max_bytes=min(MAX_IMAGE_BYTES, MAX_IMAGE_BYTES_TOTAL - total_bytes),
            allowed_types=_IMAGE_TYPES,
        )
        if got is None:
            continue
        content, media_type = got
        ext = _EXT_BY_TYPE[media_type]
        total_bytes += len(content)
        file_name = f"images/img{i}.{ext}"
        items.append(
            epub.EpubItem(
                uid=f"img{i}",
                file_name=file_name,
                media_type=media_type,
                content=content,
            )
        )
        img.set("src", file_name)
    return items


# Attribute values that make a reader execute something. EPUB readers rarely
# run JS, but "rarely" isn't "never" and the content is untrusted, so strip
# them rather than rely on the reader.
_URL_ATTRS = ("href", "src", "action", "formaction", "data", "poster", "background")
_ACTIVE_SCHEME = re.compile(r"^[\s\x00-\x20]*(javascript|vbscript|data)[\s\x00-\x20]*:", re.I)
# data:image/... is the one data: URL worth keeping: an inline image already
# works offline, which is the whole point of the book we're building.
_INLINE_IMAGE = re.compile(r"^[\s\x00-\x20]*data[\s\x00-\x20]*:\s*image/", re.I)


def _strip_active_content(doc) -> None:
    """Remove event handlers and script-bearing URLs in place.

    <script> and <style> elements are removed separately; this covers what
    survives element removal — onload/onerror attributes and javascript: URLs.
    data: is included because data:text/html is a script vector.
    """
    for el in doc.xpath("//*"):
        for name in list(el.attrib):
            if name.lower().startswith("on"):
                del el.attrib[name]
        for name in _URL_ATTRS:
            value = el.get(name)
            if value and _ACTIVE_SCHEME.match(value) and not _INLINE_IMAGE.match(value):
                del el.attrib[name]


def _split_units(doc) -> list | None:
    """Top-level section elements to become chapters, or None for a single chapter."""
    marked = [
        el for el in doc.xpath(f"//*[@{_TOC_ATTR}]")
        if not el.xpath(f"ancestor::*[@{_TOC_ATTR}]")
    ]
    if len(marked) < 2:
        marked = [s for s in doc.xpath("//section") if not s.xpath("ancestor::section")]
    return marked if len(marked) >= 2 else None


def _unit_title(el, index: int) -> str:
    for h in el.xpath(".//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6"):
        text = " ".join(h.text_content().split()).strip()
        if text:
            return text[:120]
    etype = el.get("epub:type")
    if etype:
        return etype.replace("-", " ").title()
    return f"Section {index + 1}"


def _serialize(el) -> str:
    # method="xml" for XHTML well-formedness; drop epub:type (its namespace is
    # not declared on the chapters ebooklib generates).
    xml = tostring(el, encoding="unicode", method="xml")
    return re.sub(r'\s+epub:type="[^"]*"', "", xml)


async def _fetch_cover(client: httpx.AsyncClient, url: str) -> bytes | None:
    # image_url comes from upstream article metadata, so it gets the same
    # treatment as an <img src> in the body.
    got = await fetch_bytes(
        client,
        url,
        timeout=IMAGE_FETCH_TIMEOUT,
        max_bytes=MAX_IMAGE_BYTES,
        allowed_types=_IMAGE_TYPES,
    )
    return got[0] if got else None


async def build_epub(
    title: str,
    author: str | None,
    html_content: str,
    source_url: str | None = None,
    identifier: str | None = None,
    language: str = "en",
    preserve_styles: bool = False,
    image_url: str | None = None,
    raw_cover: bool = False,
    image_client: httpx.AsyncClient | None = None,
) -> bytes:
    """Convert Readwise html_content into an EPUB.

    Splits into per-section chapters (with a nav TOC) when the source carries
    structure, else emits a single chapter. When preserve_styles is set (epub
    uploads), the source's own stylesheet is kept and scoped; otherwise content
    is normalized. A cover is always set: the hero image raw when raw_cover is
    set (epub uploads keep their designed cover), otherwise a generated cover
    (faded hero + title/author, or a clean text cover when there's no image).
    """
    book = epub.EpubBook()

    if identifier is None:
        identifier = hashlib.sha256(f"{title}:{source_url or ''}".encode()).hexdigest()[:16]
    book.set_identifier(f"later-ink-{identifier}")
    book.set_title(title)
    book.set_language(language or "en")
    if author:
        book.add_author(author)
    if source_url:
        book.add_metadata("DC", "source", source_url)

    image_items: list[epub.EpubItem] = []
    chapters_src: list[tuple[str, str]] = []
    original_css = ""
    use_orig = False

    owns_client = image_client is None
    client = image_client or httpx.AsyncClient()
    try:
        cover_src = await _fetch_cover(client, image_url) if image_url else None
        try:
            doc = fromstring(html_content)
            if preserve_styles:
                original_css = "\n".join(s.text_content() for s in doc.xpath("//style"))
            for el in doc.xpath("//script | //style"):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
            _strip_active_content(doc)

            image_items = await _embed_images(doc, client)

            units = _split_units(doc)
            if units is None:
                chapters_src = [(title, _serialize(doc))]
            else:
                chapters_src = [(_unit_title(el, i), _serialize(el)) for i, el in enumerate(units)]

            use_orig = preserve_styles and bool(original_css.strip())
        except Exception:
            logger.warning("HTML parse failed for %r; emitting fallback page", title)
            chapters_src = [(title, _fallback_html(title, source_url))]
            use_orig = False
    finally:
        if owns_client:
            await client.aclose()

    if raw_cover and cover_src:
        cover_bytes = cover_src
    elif raw_cover:
        cover_bytes = covers.make_cover(None, title, author)
    else:
        cover_bytes = covers.make_cover(cover_src, title, author)
    book.set_cover("cover.jpg", cover_bytes, create_page=True)

    # A single stylesheet linked from every chapter: the source's own (scoped)
    # for epub uploads, otherwise our normalized one.
    css_item = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=(original_css if use_orig else NORMALIZE_CSS).encode("utf-8"),
    )
    book.add_item(css_item)

    chapters = []
    for i, (ctitle, inner) in enumerate(chapters_src):
        if use_orig:
            # Readwise's original epub CSS is scoped to this container class.
            body = f'<div class="document-content epub-original-styles">{inner}</div>'
        else:
            body = inner
        xhtml = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{escape(ctitle or '')}</title></head><body>{body}</body></html>"
        )
        chapter = epub.EpubHtml(
            uid=f"chap_{i:03d}",
            title=ctitle or f"Section {i + 1}",
            file_name=f"chap_{i:03d}.xhtml",
            lang=language,
        )
        chapter.set_content(xhtml.encode("utf-8"))
        chapter.add_link(href="style/main.css", rel="stylesheet", type="text/css")
        book.add_item(chapter)
        chapters.append(chapter)

    for item in image_items:
        book.add_item(item)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    # Link a stylesheet to the nav so that, when it's shown as a readable page,
    # the table of contents renders unnumbered rather than as a numbered <ol>.
    nav = epub.EpubNav()
    nav_css = epub.EpubItem(
        uid="nav_style",
        file_name="style/nav.css",
        media_type="text/css",
        content=NAV_CSS.encode("utf-8"),
    )
    book.add_item(nav_css)
    nav.add_link(href="style/nav.css", rel="stylesheet", type="text/css")
    book.add_item(nav)
    # Open on the cover. Keep the nav as a readable first page only when there's
    # real structure to navigate — a single-chapter piece doesn't need a ToC page
    # in front of its body (the nav doc still ships for the reader's ToC menu).
    spine: list = [("cover", True)]
    if len(chapters) > 1:
        spine.append("nav")
    spine.extend(chapters)
    book.spine = spine

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
