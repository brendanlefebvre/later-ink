"""Tests for the hardening applied after the 2026-08-05 review.

Grouped by the finding each one pins down, so a future change that quietly
undoes one of them fails with an obvious name.
"""
import asyncio
import io
import zipfile

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from later_ink import fetch, main, pages
from later_ink.epub import build_epub
from later_ink.store import Store

# A genuinely public address. MockTransport intercepts before any connection,
# but fetch.py resolves the host first, so the literal has to pass that check.
PUBLIC = "93.184.216.34"


# ------------------------------------------------------------------ H1
# Fail closed rather than start with an ephemeral key and lose every stored
# token on the next restart.


def test_encryption_key_required_for_signups(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY must be set"):
        with TestClient(app=main.app):
            pass


def test_encryption_key_required_when_stripe_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "b.db"))
    monkeypatch.delenv("ALLOW_FREE_SIGNUP", raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY must be set"):
        with TestClient(app=main.app):
            pass


def test_ephemeral_key_still_fine_for_self_host(tmp_path, monkeypatch):
    # Single-user self-hosting persists no tokens, so requiring a key there
    # would be friction with no benefit.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "c.db"))
    monkeypatch.delenv("ALLOW_FREE_SIGNUP", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app) as c:
        assert c.get("/healthz").status_code == 200


def test_ephemeral_key_refused_when_users_already_exist(tmp_path, monkeypatch):
    # Signups turned off after the fact doesn't make the stored tokens readable.
    db = tmp_path / "d.db"
    Store(str(db), Fernet(Fernet.generate_key())).create_user("tok")
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.delenv("ALLOW_FREE_SIGNUP", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="already has users"):
        with TestClient(app=main.app):
            pass


def test_malformed_encryption_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "e.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-fernet-key")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        with TestClient(app=main.app):
            pass


# ------------------------------------------------------------------ H2
# Article HTML is third-party, so every <img src> in it is an SSRF attempt
# waiting to happen.


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://100.64.0.1/x",  # carrier-grade NAT
        "http://[::1]/x",
        "http://[fd00::1]/x",
        "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped loopback
        "file:///etc/passwd",
        "gopher://127.0.0.1/x",
    ],
)
def test_private_and_non_http_targets_are_blocked(url):
    with pytest.raises(ValueError):
        asyncio.run(fetch._validate(url))


def test_public_target_is_allowed():
    asyncio.run(fetch._validate(f"http://{PUBLIC}/x"))  # does not raise


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _get(handler, url, **kw):
    async def run():
        async with _client(handler) as c:
            return await fetch.fetch_bytes(
                c, url, timeout=5.0, max_bytes=kw.pop("max_bytes", 1024), **kw
            )

    return asyncio.run(run())


def test_redirect_to_private_address_is_blocked():
    # The whole point of following redirects by hand: validating only the
    # first URL is trivially bypassed with a 302.
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == PUBLIC:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/creds"})
        return httpx.Response(200, content=b"secret", headers={"content-type": "image/png"})

    assert _get(handler, f"http://{PUBLIC}/a.png") is None
    assert len(seen) == 1  # never asked for the metadata endpoint


def test_redirect_chain_is_capped():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": f"http://{PUBLIC}/next"})

    assert _get(handler, f"http://{PUBLIC}/a.png") is None
    assert len(seen) == fetch.MAX_REDIRECTS + 1  # gave up rather than looping


def test_redirect_to_public_address_is_followed():
    def handler(request):
        if request.url.path == "/a.png":
            return httpx.Response(302, headers={"location": f"http://{PUBLIC}/real.png"})
        return httpx.Response(200, content=b"img", headers={"content-type": "image/png"})

    assert _get(handler, f"http://{PUBLIC}/a.png") == (b"img", "image/png")


def test_oversize_body_is_dropped_even_when_content_length_lies():
    def handler(request):
        return httpx.Response(
            200,
            content=b"x" * 5000,
            headers={"content-type": "image/png", "content-length": "10"},
        )

    assert _get(handler, f"http://{PUBLIC}/big.png", max_bytes=1000) is None


def test_content_type_is_checked_before_use():
    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    got = _get(handler, f"http://{PUBLIC}/x", allowed_types=frozenset({"image/png"}))
    assert got is None


def test_epub_build_skips_private_image_without_failing():
    # A blocked image must degrade to a broken reference, not a failed download.
    # Record rather than raise: build_epub catches broadly, so an exception here
    # would be swallowed and the test would pass without proving anything.
    reached = []

    def handler(request):
        reached.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    async def run():
        async with _client(handler) as client:
            return await build_epub(
                title="SSRF",
                author=None,
                html_content='<img src="http://169.254.169.254/latest/meta-data/">',
                identifier="ssrf1",
                image_client=client,
            )

    data = asyncio.run(run())
    assert reached == []  # the metadata endpoint was never contacted
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "169.254.169.254" in zf.read("EPUB/chap_000.xhtml").decode()


# ------------------------------------------------------------------ M6


def test_event_handlers_and_javascript_urls_stripped():
    html = (
        '<p onclick="steal()">x</p>'
        '<img src="http://x/y.png" onerror="alert(1)">'
        '<a href="javascript:alert(1)">bad</a>'
        '<a href="https://example.com/ok">good</a>'
        '<img src="data:image/png;base64,AAAA">'
    )
    data = asyncio.run(build_epub(title="A", author=None, html_content=html, identifier="x1"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        out = zf.read("EPUB/chap_000.xhtml").decode()
    assert "onclick" not in out and "onerror" not in out
    assert "javascript:" not in out
    assert "https://example.com/ok" in out  # ordinary links survive
    assert "data:image/png;base64" in out  # inline images are the one data: to keep


# ------------------------------------------------------------------ M3


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "h.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_FREE_SIGNUP", raising=False)
    with TestClient(app=main.app) as c:
        yield c


def test_hardening_headers_present(app_client):
    h = app_client.get("/").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_csp_hashes_cover_every_inline_script(app_client):
    # If a script is edited and the hash isn't regenerated, the browser silently
    # drops it and the theme toggle stops working. The hashes are derived from
    # the same constants the page emits, so this asserts they stay in step.
    body = app_client.get("/").text
    csp = app_client.get("/").headers["content-security-policy"]
    for js in pages._INLINE_SCRIPTS:
        assert f"<script>{js}</script>" in body
        assert pages._script_hash(js) in csp
    assert body.count("<script>") == len(pages._INLINE_SCRIPTS)
    assert "'unsafe-inline'" not in csp.split("style-src")[0]  # scripts are hashed, not blanket


def test_public_pages_stay_cacheable(app_client):
    assert "no-store" not in app_client.get("/").headers.get("cache-control", "")
    assert "max-age" in app_client.get("/assets/demo.gif").headers["cache-control"]


def test_private_paths_are_never_cached(app_client, monkeypatch):
    # A catalog feed and the page that shows a secret must not land in a proxy.
    for path in ("/unknown-secret-here-xx/", "/start", "/opds/"):
        resp = app_client.get(path)
        assert resp.headers["cache-control"] == "private, no-store", path


# ------------------------------------------------------------------ H3


def test_reused_stripe_session_is_rejected_not_a_500(tmp_path, monkeypatch):
    """The UNIQUE(stripe_ref) insert is the real gate, and it must be handled.

    stripe_ref_used() is a friendlier early exit, but two concurrent /start
    requests carrying the same cs_… both pass it and the second insert raises.
    Simulated here by stubbing that check to False so the insert is what
    decides — which is exactly the state a race leaves the handler in.
    """
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "j.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("ALLOW_FREE_SIGNUP", raising=False)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)

    async def ok(session_id, key):
        return True

    async def valid(token):
        return True

    monkeypatch.setattr(main, "verify_checkout_session", ok)
    monkeypatch.setattr(main.readwise, "validate_token", valid)

    with TestClient(app=main.app, raise_server_exceptions=False) as c:
        monkeypatch.setattr(main.app.state.store, "stripe_ref_used", lambda ref: False)
        first = c.post("/start", data={"readwise_token": "t", "session_id": "cs_dup"})
        assert first.status_code == 200
        second = c.post("/start", data={"readwise_token": "t", "session_id": "cs_dup"})
        assert second.status_code == 403
        assert "already been used" in second.text


# ------------------------------------------------------------------ M5


def test_csrf_key_is_not_the_encryption_key(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "i.db"))
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    with TestClient(app=main.app):
        assert main.app.state.csrf_key != key.encode()
        assert len(main.app.state.csrf_key) == 32
        # Deterministic: the same root key must keep issuing usable tokens
        # across restarts, or every outstanding form breaks on deploy.
        assert main._derive_csrf_key(key) == main.app.state.csrf_key
