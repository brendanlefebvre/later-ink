import io
import hashlib
from html import escape

from ebooklib import epub
from lxml.html import fromstring, tostring


def _clean_html(raw_html: str) -> str:
    """Parse raw HTML and output as well-formed XML suitable for EPUB."""
    try:
        doc = fromstring(raw_html)
        for el in doc.iter("script", "style"):
            el.getparent().remove(el)
        return tostring(doc, encoding="unicode", method="xml")
    except Exception:
        return f"<p>{escape(raw_html)}</p>"


def build_epub(
    title: str,
    author: str | None,
    html_content: str,
    source_url: str | None = None,
) -> bytes:
    book = epub.EpubBook()

    uid = hashlib.sha256(f"{title}:{source_url or ''}".encode()).hexdigest()[:16]
    book.set_identifier(f"read-later-opds-{uid}")
    book.set_title(title)
    book.set_language("en")

    if author:
        book.add_author(author)
    if source_url:
        book.add_metadata("DC", "source", source_url)

    clean = _clean_html(html_content)
    xhtml = (
        f'<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>{escape(title)}</title>"
        f"<style>body {{ font-family: serif; line-height: 1.6; max-width: 40em; margin: 0 auto; padding: 1em; }}</style>"
        f"</head><body>{clean}</body></html>"
    )

    chapter = epub.EpubHtml(title=title, file_name="article.xhtml")
    chapter.set_content(xhtml.encode("utf-8"))
    book.add_item(chapter)

    book.toc = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]

    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
