import base64
import json

import httpx
import pytest

from later_ink.connectors.base import ArticleUnavailable, UpstreamError
from later_ink.connectors.instapaper import (
    _decode_article_id,
    _encode_article_id,
    _OAuth1Auth,
    _parse_error_envelope,
    _raise_for_get_text_error,
    _raise_for_json_error,
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


def test_parse_error_envelope_detects_list_and_bare_forms():
    assert _parse_error_envelope('[{"type":"error","error_code":1241}]')["error_code"] == 1241
    assert _parse_error_envelope('{"type":"error","error_code":1550}')["error_code"] == 1550


def test_parse_error_envelope_returns_none_for_html_and_non_error_json():
    assert _parse_error_envelope("<article><p>Body</p></article>") is None
    assert _parse_error_envelope('[{"type":"bookmark","bookmark_id":1}]') is None


def test_raise_for_json_error_maps_rate_limit_and_suspension():
    with pytest.raises(UpstreamError) as e1:
        _raise_for_json_error([{"type": "error", "error_code": 1040}])
    assert e1.value.status == 429
    with pytest.raises(UpstreamError) as e2:
        _raise_for_json_error([{"type": "error", "error_code": 1042}])
    assert e2.value.status == 403


def test_raise_for_json_error_passes_clean_list():
    assert _raise_for_json_error([{"type": "bookmark", "bookmark_id": 1}]) is None


def test_raise_for_json_error_unknown_code_is_502():
    with pytest.raises(UpstreamError) as e:
        _raise_for_json_error([{"type": "error", "error_code": 1500}])
    assert e.value.status == 502


def test_raise_for_get_text_error_maps_codes():
    with pytest.raises(ArticleUnavailable) as e_missing:
        _raise_for_get_text_error({"type": "error", "error_code": 1241})
    assert e_missing.value.status == 404

    for code in (1041, 1220, 1221, 1550):
        with pytest.raises(ArticleUnavailable) as e:
            _raise_for_get_text_error({"type": "error", "error_code": code})
        assert e.value.status == 422

    with pytest.raises(UpstreamError) as e_rate:
        _raise_for_get_text_error({"type": "error", "error_code": 1040})
    assert e_rate.value.status == 429


def test_raise_for_get_text_error_unknown_code_is_article_unavailable_422():
    with pytest.raises(ArticleUnavailable) as e:
        _raise_for_get_text_error({"type": "error", "error_code": 9999})
    assert e.value.status == 422
