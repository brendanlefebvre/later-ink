import io
import hashlib
import logging
from html import escape

import httpx
from ebooklib import epub
from lxml.html import fromstring, tostring

logger = logging.getLogger(__name__)

# One image-heavy article must not blow up a download over a Kobo's wifi.
MAX_IMAGES = 20
MAX_IMAGE_BYTES_TOTAL = 10 * 1024 * 1024
IMAGE_FETCH_TIMEOUT = 10.0

_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


def _fallback_html(title: str, source_url: str | None) -> str:
    link = (
        f'<p>Read it at the source: <a href="{escape(source_url)}">{escape(source_url)}</a></p>'
        if source_url
        else ""
    )
    return (
        f"<h1>{escape(title)}</h1>"
        f"<p>This article could not be converted for offline reading.</p>{link}"
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
        try:
            resp = await client.get(src, timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except Exception:
            logger.debug("image fetch failed: %s", src)
            continue
        media_type = resp.headers.get("content-type", "").split(";")[0].strip()
        ext = _EXT_BY_TYPE.get(media_type)
        if ext is None or len(resp.content) == 0:
            continue
        if total_bytes + len(resp.content) > MAX_IMAGE_BYTES_TOTAL:
            break
        total_bytes += len(resp.content)
        file_name = f"images/img{i}.{ext}"
        items.append(
            epub.EpubItem(
                uid=f"img{i}",
                file_name=file_name,
                media_type=media_type,
                content=resp.content,
            )
        )
        img.set("src", file_name)
    return items


async def build_epub(
    title: str,
    author: str | None,
    html_content: str,
    source_url: str | None = None,
    identifier: str | None = None,
    language: str = "en",
    image_client: httpx.AsyncClient | None = None,
) -> bytes:
    book = epub.EpubBook()

    if identifier is None:
        identifier = hashlib.sha256(f"{title}:{source_url or ''}".encode()).hexdigest()[:16]
    book.set_identifier(f"read-later-opds-{identifier}")
    book.set_title(title)
    book.set_language(language or "en")

    if author:
        book.add_author(author)
    if source_url:
        book.add_metadata("DC", "source", source_url)

    image_items: list[epub.EpubItem] = []
    try:
        doc = fromstring(html_content)
        for el in doc.iter("script", "style"):
            el.getparent().remove(el)
        if image_client is not None:
            image_items = await _embed_images(doc, image_client)
        else:
            async with httpx.AsyncClient() as client:
                image_items = await _embed_images(doc, client)
        clean = tostring(doc, encoding="unicode", method="xml")
    except Exception:
        logger.warning("HTML parse failed for %r; emitting fallback page", title)
        clean = _fallback_html(title, source_url)

    xhtml = (
        f'<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>{escape(title)}</title>"
        f"<style>body {{ font-family: serif; line-height: 1.6; max-width: 40em; margin: 0 auto; padding: 1em; }} img {{ max-width: 100%; }}</style>"
        f"</head><body>{clean}</body></html>"
    )

    chapter = epub.EpubHtml(title=title, file_name="article.xhtml")
    chapter.set_content(xhtml.encode("utf-8"))
    book.add_item(chapter)
    for item in image_items:
        book.add_item(item)

    book.toc = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
