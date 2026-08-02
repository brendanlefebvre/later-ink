import asyncio
import io
import re
import zipfile

import httpx

from read_later_opds.epub import build_epub

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

CH0 = "EPUB/chap_000.xhtml"


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


def test_inline_toc_ordered_list_becomes_unordered():
    # An in-body table of contents (anchors to page fragments), including a
    # nested sublist, should render unnumbered at every level so its 1./2./3.
    # doesn't duplicate the section headings.
    html = (
        "<ol>"
        "<li><a href='#one'>One</a>"
        "<ol><li><a href='#one-a'>One A</a></li></ol>"
        "</li>"
        "<li><a href='#two'>Two</a></li>"
        "<li><a href='#three'>Three</a></li>"
        "</ol>"
        "<h2 id='one'>One</h2><h3 id='one-a'>One A</h3>"
        "<h2 id='two'>Two</h2><h2 id='three'>Three</h2><p>alpha</p>"
    )
    data = asyncio.run(build_epub(title="Toc", author=None, html_content=html, identifier="toc1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        chapter = zf.read(CH0).decode()
        assert "<ul" in chapter  # converted to unordered
        assert "<ol" not in chapter  # both outer and nested levels renumbered
        for frag in ("#one", "#one-a", "#two", "#three"):
            assert frag in chapter  # every link preserved


def test_inline_toc_detected_by_nav_and_class():
    # <nav> wrapper and a toc class are explicit signals, even without anchors.
    html = (
        "<nav><ol><li>Intro</li><li>Body</li></ol></nav>"
        "<ol class='table-of-contents'><li>A</li><li>B</li></ol>"
        "<p>text</p>"
    )
    data = asyncio.run(build_epub(title="Nav", author=None, html_content=html, identifier="nav1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert b"<ol" not in zf.read(CH0)  # both lists de-numbered


def test_genuine_ordered_list_kept_numbered():
    # A real enumerated list (no anchors, no toc markers) must stay an <ol>.
    html = "<h1>Steps</h1><ol><li>First do this</li><li>Then that</li></ol>"
    data = asyncio.run(build_epub(title="Steps", author=None, html_content=html, identifier="ord1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert b"<ol" in zf.read(CH0)  # numbering preserved


def test_numbered_steps_with_inline_links_kept_numbered():
    # A genuine procedure whose steps merely contain a fragment link is prose,
    # not a ToC — it must stay ordered.
    html = (
        "<h1>Steps</h1><ol>"
        "<li>First, open the <a href='#config'>config</a> file</li>"
        "<li>Then restart the <a href='#daemon'>daemon</a></li>"
        "</ol>"
    )
    data = asyncio.run(build_epub(title="Proc", author=None, html_content=html, identifier="proc1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert b"<ol" in zf.read(CH0)  # numbering preserved despite inline links


def test_notoc_class_does_not_trigger_conversion():
    # Marker matching is token-based, so "notoc" must not be read as "toc".
    html = "<ol class='notoc'><li>First</li><li>Second</li></ol>"
    data = asyncio.run(build_epub(title="NoToc", author=None, html_content=html, identifier="notoc1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert b"<ol" in zf.read(CH0)  # stays ordered


def test_images_embedded_and_rewritten():
    async def run():
        async with _mock_client() as client:
            return await build_epub(
                title="Pics",
                author=None,
                html_content='<p>x</p><img src="https://example.com/a.png">',
                identifier="img1",
                image_client=client,
            )

    data = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert any(n.endswith(".png") for n in zf.namelist())
        xhtml = zf.read(CH0).decode()
        assert "https://example.com/a.png" not in xhtml
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
                image_url="https://example.com/cover.png",
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
                html_content='<img src="https://example.com/gone.png">',
                identifier="img2",
                image_client=client,
            )

    data = asyncio.run(run())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "https://example.com/gone.png" in zf.read(CH0).decode()
