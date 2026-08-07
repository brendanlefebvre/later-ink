"""Outbound HTTP for attacker-controlled URLs (article images, cover art).

Article HTML comes from upstream services, so every `<img src>` in it — and
every `image_url` on an article — is a URL a third party chose. Fetching those
without a guard turns an image tag into a server-side request forgery
primitive: the container can reach cloud metadata (169.254.169.254), other
services on a private network, and its own localhost ports.

So every fetch here goes through `fetch_bytes`, which:
  - allows only http/https,
  - resolves the host and refuses any non-global address,
  - follows redirects itself, re-validating each hop (a 302 to
    169.254.169.254 is the obvious bypass of a check done only on the first
    URL), up to MAX_REDIRECTS,
  - checks Content-Type before buffering, and
  - streams with a hard byte cap, so a hostile server can't feed us a
    multi-gigabyte body.

Residual risk: DNS rebinding. We validate the addresses the resolver returns,
then httpx resolves again when it connects, so a name whose record flips
between the two lookups is still reachable. Closing that means pinning the
validated IP into the connection and carrying the hostname for TLS separately,
which is a lot of machinery for this threat model. Documented, not fixed.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 2


def _unmap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Reduce an IPv4-mapped IPv6 address to its IPv4 form.

    ::ffff:127.0.0.1 routes to loopback, but whether IPv6Address.is_global
    looks through the mapping depends on the Python version. Unmap first so
    the check is the same everywhere.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


async def _validate(url: str) -> None:
    """Raise ValueError unless `url` is http(s) and resolves only to public IPs."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"scheme not allowed: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise ValueError("no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise ValueError(f"could not resolve {host}") from e
    if not infos:
        raise ValueError(f"could not resolve {host}")

    # Every address must be public: a name resolving to both a public and a
    # private address must not get through on the strength of the public one.
    for info in infos:
        ip = _unmap(ipaddress.ip_address(info[4][0]))
        if not ip.is_global:
            raise ValueError(f"{host} resolves to non-public address {ip}")


async def fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    allowed_types: frozenset[str] | None = None,
) -> tuple[bytes, str] | None:
    """Fetch `url`, returning (body, media_type), or None if it can't be used.

    Every failure — blocked target, transport error, wrong type, oversize
    body — is a None. Callers treat a missing image as cosmetic, so there is
    nothing useful to distinguish.
    """
    for _ in range(MAX_REDIRECTS + 1):
        try:
            await _validate(url)
        except ValueError as e:
            logger.debug("blocked fetch %s: %s", _redact(url), e)
            return None

        try:
            async with client.stream(
                "GET", url, timeout=timeout, follow_redirects=False
            ) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    # Resolve relative redirects against the URL we just asked for.
                    url = str(resp.url.join(location))
                    continue
                resp.raise_for_status()

                media_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if allowed_types is not None and media_type not in allowed_types:
                    logger.debug("skipping %s: content-type %r", _redact(url), media_type)
                    return None

                # Trust Content-Length only to reject early; the cap below is
                # what actually bounds memory, since the header can lie.
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    logger.debug("skipping %s: declared %s bytes", _redact(url), declared)
                    return None

                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        logger.debug("skipping %s: body exceeds %d bytes", _redact(url), max_bytes)
                        return None
                if not body:
                    return None
                return bytes(body), media_type
        except Exception:
            logger.debug("fetch failed: %s", _redact(url), exc_info=True)
            return None

    logger.debug("too many redirects: %s", _redact(url))
    return None


def _redact(url: str) -> str:
    """Make a third-party URL safe to log.

    Three problems, all because the URL is text an attacker chose. The query
    string can carry a token, so it's dropped along with the fragment. So can
    userinfo (`http://user:pass@host/`), so the authority is rebuilt from just
    the host and port rather than reusing netloc. And a newline in the value
    would let it forge whole log lines, so control characters are escaped
    rather than passed through.
    """
    try:
        p = urlsplit(url)
        authority = p.hostname or ""
        if p.port:
            authority = f"{authority}:{p.port}"
        cleaned = urlunsplit((p.scheme, authority, p.path, "", ""))
    except ValueError:
        return "(unparseable url)"
    return _LOG_UNSAFE.sub(lambda m: f"\\x{ord(m.group()):02x}", cleaned)[:300]


_LOG_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
