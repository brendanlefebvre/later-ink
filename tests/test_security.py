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
from later_ink.epub import _sanitize_svg, build_epub
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


def _build(html: str, ident: str) -> str:
    """Build a book from `html` and return chapter 0. Never touches the network:
    a mock client keeps the <img> fetches (and their DNS lookups) local."""

    def handler(request):
        return httpx.Response(404)

    async def run():
        async with _client(handler) as client:
            return await build_epub(
                title="A", author=None, html_content=html, identifier=ident,
                image_client=client,
            )

    with zipfile.ZipFile(io.BytesIO(asyncio.run(run()))) as zf:
        return zf.read("EPUB/chap_000.xhtml").decode()


def test_event_handlers_and_javascript_urls_stripped():
    out = _build(
        '<p onclick="steal()">x</p>'
        f'<img src="http://{PUBLIC}/y.png" onerror="alert(1)">'
        '<a href="javascript:alert(1)">bad</a>'
        '<a href="https://example.com/ok">good</a>'
        '<img src="data:image/png;base64,AAAA">',
        "x1",
    )
    assert "onclick" not in out and "onerror" not in out
    assert "javascript:" not in out
    assert "https://example.com/ok" in out  # ordinary links survive
    assert "data:image/png;base64" in out  # inline images are the one data: to keep


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "  JaVaScRiPt:alert(1)",
        "java&#9;script:alert(1)",  # tab, as an entity
        "java&#10;script:alert(1)",  # newline
        "java&#13;script:alert(1)",  # carriage return
        "&#106;avascript:alert(1)",  # first letter as an entity
        "vbscript:msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_active_urls_stripped_including_control_char_evasions(href):
    # A reader drops control characters from a URL before resolving it, so
    # "java&#9;script:" is live javascript: by the time it matters — matching
    # the literal scheme alone lets the entity form through.
    out = _build(f'<a href="{href}">x</a>', "ev")
    assert "alert" not in out and "msgbox" not in out, out


def test_fetched_svg_is_sanitized():
    # SVG is the one allowlisted image type that is also a document format.
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink">'
        b"<script>alert(1)</script>"
        b'<a xlink:href="javascript:alert(2)"><circle r="9" onclick="alert(3)"/></a>'
        b"<foreignObject><body onload=\"alert(4)\"/></foreignObject>"
        b'<rect width="10" height="10"/>'
        b"</svg>"
    )

    def handler(request):
        return httpx.Response(200, content=svg, headers={"content-type": "image/svg+xml"})

    async def run():
        async with _client(handler) as client:
            return await build_epub(
                title="S", author=None,
                html_content=f'<img src="http://{PUBLIC}/a.svg">',
                identifier="svg1", image_client=client,
            )

    with zipfile.ZipFile(io.BytesIO(asyncio.run(run()))) as zf:
        names = [n for n in zf.namelist() if n.endswith(".svg")]
        assert names, "svg should still be embedded, just defanged"
        out = zf.read(names[0]).decode()
    assert "<script" not in out and "foreignObject" not in out
    assert "onclick" not in out and "onload" not in out
    assert "javascript:" not in out
    assert "<rect" in out  # the actual drawing survives


def test_malformed_svg_is_dropped_not_embedded():
    def handler(request):
        return httpx.Response(
            200, content=b"<svg><unclosed>", headers={"content-type": "image/svg+xml"}
        )

    async def run():
        async with _client(handler) as client:
            return await build_epub(
                title="S", author=None,
                html_content=f'<img src="http://{PUBLIC}/bad.svg">',
                identifier="svg2", image_client=client,
            )

    with zipfile.ZipFile(io.BytesIO(asyncio.run(run()))) as zf:
        assert not [n for n in zf.namelist() if n.endswith(".svg")]


def test_svg_external_entities_are_not_resolved():
    # Parsing attacker-supplied XML with entity resolution on would be an XXE
    # and a file-read primitive.
    svg = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
    )

    def handler(request):
        return httpx.Response(200, content=svg, headers={"content-type": "image/svg+xml"})

    async def run():
        async with _client(handler) as client:
            return await build_epub(
                title="S", author=None,
                html_content=f'<img src="http://{PUBLIC}/xxe.svg">',
                identifier="svg3", image_client=client,
            )

    with zipfile.ZipFile(io.BytesIO(asyncio.run(run()))) as zf:
        names = [n for n in zf.namelist() if n.endswith(".svg")]
        # Assert it was embedded before asserting what it contains: if the SVG
        # were dropped, an empty string would satisfy the checks below and the
        # test would pass without exercising entity resolution at all.
        assert names, "SVG should be embedded, so the entity check is meaningful"
        out = zf.read(names[0]).decode(errors="replace")
    assert "root:" not in out and "/bin/" not in out
    assert "&xxe;" not in out  # not expanded, and not left as a live reference


def test_inline_svg_data_url_is_not_treated_as_an_image():
    # data:image/svg+xml is a document, and it is never fetched, so it never
    # reaches _sanitize_svg. Allowing it as an "image" would route active
    # content around the one check that would have caught it.
    out = _build(
        '<img src="data:image/svg+xml,<svg onload=alert(1)></svg>">'
        '<img src="data:image/png;base64,AAAA">',
        "isvg",
    )
    assert "svg+xml" not in out
    assert "data:image/png;base64" in out  # raster inline images still fine


def test_svg_style_and_smil_are_removed():
    # @import turns an embedded book asset back into an outbound request, and
    # SMIL writes attributes at render time, where attribute stripping (which
    # only sees the static tree) cannot follow.
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<style>@import url("https://evil.example/x.css");</style>'
        b'<animate attributeName="xlink:href" to="javascript:alert(1)"/>'
        b'<set attributeName="href" to="javascript:alert(2)"/>'
        b'<rect width="10" height="10"/></svg>'
    )
    out = _sanitize_svg(svg).decode()
    assert "@import" not in out and "evil.example" not in out
    assert "animate" not in out and "<set" not in out
    assert "javascript:" not in out
    assert "<rect" in out


def test_sanitize_svg_survives_any_lxml_failure():
    # Escaping here would reach build_epub's outer handler and replace the whole
    # article with the fallback page over a single bad image.
    for bad in (b"", b"\x00\x01\x02", b"<svg><unclosed>", b"not xml at all", b"<?xml?>"):
        assert _sanitize_svg(bad) is None


def test_redacted_url_drops_secrets_and_cannot_forge_log_lines():
    assert "\n" not in fetch._redact("http://h/a\nINFO fake log line")
    assert "secret" not in fetch._redact("http://h/a?token=secret")
    # userinfo is the same secret-leak class as the query string
    redacted = fetch._redact("http://user:s3cr3t@host/path")
    assert "s3cr3t" not in redacted and "user" not in redacted
    assert "host/path" in redacted
    assert fetch._redact("http://host:8443/p") == "http://host:8443/p"  # port kept


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
