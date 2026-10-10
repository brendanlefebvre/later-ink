"""One-time Instapaper token minting via xAuth.

Run once, out of band, to exchange an Instapaper username/password for the
OAuth token pair the connector uses. The password is never stored by the app.

    python -m later_ink.instapaper_auth
"""

import asyncio
import getpass
import os
import urllib.parse

import httpx
import oauthlib.oauth1

from .connectors.base import UpstreamError

BASE_URL = "https://www.instapaper.com/api/1"


async def mint_tokens(
    consumer_key: str,
    consumer_secret: str,
    username: str,
    password: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Exchange username/password for (oauth_token, oauth_token_secret).

    xAuth: a signed POST to oauth/access_token with x_auth_* params. The
    response is url-encoded, not JSON.
    """
    params = {
        "x_auth_mode": "client_auth",
        "x_auth_username": username,
        "x_auth_password": password,
    }
    body = urllib.parse.urlencode(params)
    oauth = oauthlib.oauth1.Client(
        consumer_key,
        client_secret=consumer_secret,
        signature_method=oauthlib.oauth1.SIGNATURE_HMAC_SHA1,
        signature_type=oauthlib.oauth1.SIGNATURE_TYPE_AUTH_HEADER,
    )
    uri, headers, signed_body = oauth.sign(
        f"{BASE_URL}/oauth/access_token",
        http_method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(uri, content=signed_body, headers=headers)
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Instapaper: {type(e).__name__}") from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise UpstreamError(
            f"Instapaper rejected the credentials ({resp.status_code})", resp.status_code
        )
    parsed = urllib.parse.parse_qs(resp.text)
    try:
        return parsed["oauth_token"][0], parsed["oauth_token_secret"][0]
    except (KeyError, IndexError) as e:
        raise UpstreamError("Instapaper returned an unexpected auth response") from e


def main() -> None:
    consumer_key = os.environ.get("INSTAPAPER_CONSUMER_KEY") or input("Consumer key: ").strip()
    consumer_secret = (
        os.environ.get("INSTAPAPER_CONSUMER_SECRET") or getpass.getpass("Consumer secret: ")
    )
    username = input("Instapaper username (email): ").strip()
    password = getpass.getpass("Instapaper password: ")
    token, secret = asyncio.run(
        mint_tokens(consumer_key, consumer_secret, username, password)
    )
    print("\nAdd these to your environment:\n")
    print(f"INSTAPAPER_OAUTH_TOKEN={token}")
    print(f"INSTAPAPER_OAUTH_TOKEN_SECRET={secret}")


if __name__ == "__main__":
    main()
