import asyncio
import hashlib
import io
import re
import zipfile
from datetime import datetime

import httpx

from later_ink.epub import ZIP_EPOCH, _pin_zip_timestamps, build_epub

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

CH0 = "EPUB/chap_000.xhtml"

# Image fetches now resolve the host and refuse non-public addresses
# (fetch.py). A public IP literal keeps these tests off DNS; MockTransport
# means nothing is actually connected to.


def _mock_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chapter_files(zf: zipfile.ZipFile) -> list[str]:
    return sorted(n for n in zf.namelist() if n.startswith("EPUB/chap_"))


def _spine_idrefs(zf: zipfile.ZipFile) -> list[str]:
    opf = zf.read("EPUB/content.opf").decode()
    return re.findall(r'<itemref[^>]*\bidref="([^"]+)"', opf)


def test_single_chapter_epub():
    data = asyncio.run(
        build_epub(title="Test", author="Ann", html_content="<h1>Hi</h1><p>Body</p>", identifier="abc123")
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert _chapter_files(zf) == [CH0]  # no structure -> one chapter
        assert b"Body" in zf.read(CH0)


def test_single_chapter_opens_on_cover_without_toc_page():
    # One-entry pieces (like a plain article) shouldn't lead with a pointless
    # one-item ToC page; they should open straight on the cover.
    data = asyncio.run(
        build_epub(title="Solo", author="Ann", html_content="<h1>Hi</h1><p>Body</p>", identifier="solo1")
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert _spine_idrefs(zf) == ["cover", "chap_000"]  # cover first, no nav page
        assert "EPUB/cover.xhtml" in zf.namelist()  # visible cover page rendered


def test_multi_chapter_keeps_toc_page_after_cover():
    html = (
        "<div>"
        "<section data-rw-epub-toc='rw-1'><h2>One</h2><p>alpha</p></section>"
        "<section data-rw-epub-toc='rw-2'><h2>Two</h2><p>beta</p></section>"
        "</div>"
    )
    data = asyncio.run(build_epub(title="Book", author="Auth", html_content=html, identifier="multi1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # cover opens the book; the ToC page stays because there's real structure
        assert _spine_idrefs(zf) == ["cover", "nav", "chap_000", "chap_001"]
        assert "EPUB/cover.xhtml" in zf.namelist()


def test_sections_split_into_chapters_with_toc():
    html = (
        "<div>"
        "<section data-rw-epub-toc='rw-1'><h2>Chapter One</h2><p>alpha</p></section>"
        "<section data-rw-epub-toc='rw-2'><h2>Chapter Two</h2><p>beta</p></section>"
        "<section data-rw-epub-toc='rw-3'><h2>Chapter Three</h2><p>gamma</p></section>"
        "</div>"
    )
    data = asyncio.run(build_epub(title="Book", author="Auth", html_content=html, identifier="bk1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        chapters = _chapter_files(zf)
        assert len(chapters) == 3  # one file per section
        assert b"alpha" in zf.read("EPUB/chap_000.xhtml")
        assert b"gamma" in zf.read("EPUB/chap_002.xhtml")
        # nav lists the chapter titles pulled from the headings
        nav = zf.read("EPUB/nav.xhtml").decode()
        assert "Chapter One" in nav and "Chapter Three" in nav


def test_preserve_styles_keeps_original_css():
    html = (
        "<div><style>.verse { text-align: center; }</style>"
        "<section data-rw-epub-toc='rw-1'><h2>I</h2><p class='verse'>x</p></section>"
        "<section data-rw-epub-toc='rw-2'><h2>II</h2><p>y</p></section></div>"
    )
    data = asyncio.run(
        build_epub(title="Styled", author=None, html_content=html, identifier="s1", preserve_styles=True)
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert b"text-align: center" in zf.read("EPUB/style/main.css")  # original CSS kept
        chapter = zf.read("EPUB/chap_000.xhtml").decode()
        assert "epub-original-styles" in chapter  # scoped container present
        assert "main.css" in chapter  # linked, not inlined


def test_normalize_strips_original_styles_by_default():
    html = "<div><style>.verse{color:red}</style><p>plain</p></div>"
    data = asyncio.run(build_epub(title="Norm", author=None, html_content=html, identifier="n1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        css = zf.read("EPUB/style/main.css")
        assert b"color:red" not in css  # source style dropped
        assert b"max-width: 40em" in css  # our normalized stylesheet used instead
        assert b"color:red" not in zf.read(CH0)


def test_nav_page_rendered_unnumbered():
    # Multi-chapter books show the nav as a readable page. It must not read as a
    # numbered list, so the nav links a stylesheet that strips list numbering,
    # while the markup stays a spec-compliant <ol> for the reader's ToC menu.
    html = (
        "<div>"
        "<section data-rw-epub-toc='1'><h2>One</h2><p>a</p></section>"
        "<section data-rw-epub-toc='2'><h2>Two</h2><p>b</p></section>"
        "</div>"
    )
    data = asyncio.run(build_epub(title="Book", author=None, html_content=html, identifier="navcss1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "EPUB/style/nav.css" in zf.namelist()
        nav = zf.read("EPUB/nav.xhtml").decode()
        assert 'href="style/nav.css"' in nav  # nav links the unnumbering stylesheet
        assert "list-style: none" in zf.read("EPUB/style/nav.css").decode()
        assert "<ol" in nav  # markup stays a spec-compliant ordered list


def test_images_embedded_and_rewritten():
    async def run():
        async with _mock_client() as client:
            return await build_epub(
                title="Pics",
                author=None,
                html_content='<p>x</p><img src="https://93.184.216.34/a.png">',
                identifier="img1",
                image_client=client,
            )

    data = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert any(n.endswith(".png") for n in zf.namelist())
        xhtml = zf.read(CH0).decode()
        assert "https://93.184.216.34/a.png" not in xhtml
        assert "images/img0.png" in xhtml


def _cover_files(zf):
    return [n for n in zf.namelist() if "cover" in n.lower() and n.lower().endswith((".jpg", ".jpeg", ".png"))]


def test_generated_cover_present_without_image():
    data = asyncio.run(build_epub(title="No Image", author="A", html_content="<p>x</p>", identifier="cov1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        covers = _cover_files(zf)
        assert covers, zf.namelist()
        assert zf.read(covers[0])[:2] == b"\xff\xd8"  # generated JPEG


def test_raw_cover_passes_original_image_through():
    async def run():
        async with _mock_client() as client:
            return await build_epub(
                title="Book",
                author="A",
                html_content=(
                    "<section data-rw-epub-toc='1'><h2>I</h2><p>x</p></section>"
                    "<section data-rw-epub-toc='2'><h2>II</h2><p>y</p></section>"
                ),
                identifier="raw1",
                preserve_styles=True,
                image_url="https://93.184.216.34/cover.png",
                raw_cover=True,
                image_client=client,
            )

    data = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        covers = _cover_files(zf)
        assert covers
        assert zf.read(covers[0]) == PNG_BYTES  # original bytes, not regenerated


def test_image_fetch_failure_keeps_remote_ref():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await build_epub(
                title="Broken",
                author=None,
                html_content='<img src="https://93.184.216.34/gone.png">',
                identifier="img2",
                image_client=client,
            )

    data = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "https://93.184.216.34/gone.png" in zf.read(CH0).decode()


def test_epub_identifier_uses_later_ink_prefix():
    data = asyncio.run(
        build_epub(title="Test", author="Ann", html_content="<p>Body</p>", identifier="abc123")
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        opf = zf.read("EPUB/content.opf").decode()
        assert "later-ink-abc123" in opf
        assert "read-later-opds" not in opf


def _zip_with_mtime(dt: tuple[int, int, int, int, int, int]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mt = zipfile.ZipInfo("mimetype", date_time=dt)
        mt.compress_type = zipfile.ZIP_STORED
        z.writestr(mt, b"application/epub+zip")
        z.writestr(zipfile.ZipInfo("EPUB/x.xhtml", date_time=dt), b"<html/>")
    return buf.getvalue()


def _partial_md5(data: bytes) -> str:
    """KOReader's kosync document hash (frontend/util.lua partialMD5).

    Twelve 1 KiB samples at exponentially increasing offsets. Binary matching
    is kosync's default, so this is the function that decides whether reading
    progress syncs between two devices.
    """
    h = hashlib.md5()
    for off in [0] + [(1024 << (2 * i)) & 0xFFFFFFFF for i in range(11)]:
        if off >= len(data):
            break
        h.update(data[off : off + 1024])
    return h.hexdigest()


def test_pin_zip_timestamps_normalizes_differing_mtimes():
    # Two archives identical but for their entry mtimes must normalize to the
    # same bytes. This is the test that fails loudly if the pinning is dropped;
    # comparing two live builds cannot do that job, because two builds inside
    # the same clock second are already identical and the assertion passes
    # against completely unpinned code.
    a = _zip_with_mtime((2026, 8, 14, 10, 0, 0))
    b = _zip_with_mtime((2026, 8, 14, 10, 0, 2))
    assert a != b
    assert _pin_zip_timestamps(a) == _pin_zip_timestamps(b)


def test_pin_zip_timestamps_keeps_mimetype_first_and_stored():
    pinned = _pin_zip_timestamps(_zip_with_mtime((2026, 8, 14, 10, 0, 0)))
    zf = zipfile.ZipFile(io.BytesIO(pinned))
    assert zf.namelist()[0] == "mimetype"
    assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_build_epub_pins_every_entry_mtime():
    data = asyncio.run(build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d1"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert [i.date_time for i in zf.infolist()] == [ZIP_EPOCH] * len(zf.infolist())


def test_build_epub_is_byte_identical_across_builds():
    def one():
        return asyncio.run(
            build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d2")
        )

    a, b = one(), one()
    assert a == b
    assert _partial_md5(a) == _partial_md5(b)


def test_dcterms_modified_uses_content_date():
    data = asyncio.run(
        build_epub(
            title="T",
            author="A",
            html_content="<p>x</p>",
            identifier="d3",
            content_date=datetime(2025, 3, 4, 5, 6, 7),
        )
    )
    opf = zipfile.ZipFile(io.BytesIO(data)).read("EPUB/content.opf").decode()
    # Two loose assertions rather than one exact element string: lxml's
    # attribute ordering and namespace prefixing are not worth pinning here.
    assert 'property="dcterms:modified"' in opf
    assert "2025-03-04T05:06:07Z" in opf


def test_dcterms_modified_falls_back_to_sentinel():
    data = asyncio.run(build_epub(title="T", author="A", html_content="<p>x</p>", identifier="d4"))
    opf = zipfile.ZipFile(io.BytesIO(data)).read("EPUB/content.opf").decode()
    assert "1980-01-01T00:00:00Z" in opf
