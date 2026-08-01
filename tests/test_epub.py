import asyncio
import io
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


def test_single_chapter_epub():
    data = asyncio.run(
        build_epub(title="Test", author="Ann", html_content="<h1>Hi</h1><p>Body</p>", identifier="abc123")
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert _chapter_files(zf) == [CH0]  # no structure -> one chapter
        assert b"Body" in zf.read(CH0)


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
