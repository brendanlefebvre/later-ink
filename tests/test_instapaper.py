import base64
import json

import httpx
import pytest

from later_ink.connectors.base import ArticleUnavailable
from later_ink.connectors.instapaper import (
    _decode_article_id,
    _encode_article_id,
    _OAuth1Auth,
)


def test_oauth1_auth_signs_with_pinned_nonce_and_timestamp():
    auth = _OAuth1Auth("ck", "cs", "ot", "ots")
    # Pin nonce/timestamp so the signature is deterministic and we assert the
    # real base-string construction, not merely "a header exists".
    auth._client.nonce = "pinned-nonce"
    auth._client.timestamp = "1700000000"

    request = httpx.Request(
        "POST",
        "https://www.instapaper.com/api/1/bookmarks/list",
        data={"folder_id": "unread", "limit": "500"},
    )
    flow = auth.auth_flow(request)
    signed = next(flow)

    header = signed.headers["Authorization"]
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in header
    assert 'oauth_token="ot"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_nonce="pinned-nonce"' in header
    assert 'oauth_timestamp="1700000000"' in header
    assert "oauth_signature=" in header


def test_article_id_round_trips():
    token = _encode_article_id("42", 1735779845, "Some Title", "https://example.com/a")
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "https://example.com/a")


def test_article_id_round_trips_empty_url():
    token = _encode_article_id("42", 1735779845, "Some Title", None)
    assert _decode_article_id(token) == ("42", 1735779845, "Some Title", "")


def test_article_id_token_is_url_path_safe():
    token = _encode_article_id("42", 1735779845, "Café — déjà vu", "https://example.com/a?x=1")
    # base64url alphabet only (plus no padding); safe to drop into a URL path.
    assert all(c.isalnum() or c in "-_" for c in token)


def test_decode_rejects_malformed_token():
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id("!!!not-base64!!!")
    assert exc.value.status == 404


def test_decode_rejects_wellformed_base64_missing_fields():
    bad = base64.urlsafe_b64encode(json.dumps({"i": "42"}).encode()).decode().rstrip("=")
    with pytest.raises(ArticleUnavailable) as exc:
        _decode_article_id(bad)
    assert exc.value.status == 404
