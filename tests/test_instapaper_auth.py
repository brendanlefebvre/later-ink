import asyncio

import httpx
import pytest

from later_ink.connectors.base import UpstreamError
from later_ink.instapaper_auth import mint_tokens


def test_mint_tokens_parses_url_encoded_pair():
    def handler(request):
        assert request.url.path.endswith("/oauth/access_token")
        assert request.headers["Authorization"].startswith("OAuth ")
        body = request.content.decode()
        assert "x_auth_mode=client_auth" in body
        return httpx.Response(200, text="oauth_token=TOK&oauth_token_secret=SEC")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            return await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    assert asyncio.run(go()) == ("TOK", "SEC")


def test_mint_tokens_rejects_bad_status():
    def handler(request):
        return httpx.Response(401, text="Invalid xAuth credentials")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    with pytest.raises(UpstreamError) as e:
        asyncio.run(go())
    assert e.value.status == 401


def test_mint_tokens_rejects_unexpected_body():
    def handler(request):
        return httpx.Response(200, text="nothing useful here")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def go():
        try:
            await mint_tokens("ck", "cs", "user", "pw", client=client)
        finally:
            await client.aclose()

    with pytest.raises(UpstreamError):
        asyncio.run(go())
