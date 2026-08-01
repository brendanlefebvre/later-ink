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


def _mock_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_basic_epub():
    data = asyncio.run(
        build_epub(
            title="Test",
            author="Ann",
            html_content="<h1>Hi</h1><p>Body</p>",
            identifier="abc123",
        )
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "EPUB/article.xhtml" in names
        assert b"Body" in zf.read("EPUB/article.xhtml")


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
        xhtml = zf.read("EPUB/article.xhtml").decode()
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
        xhtml = zf.read("EPUB/article.xhtml").decode()
        assert "https://example.com/gone.png" in xhtml
