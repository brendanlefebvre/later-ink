import re

import pytest

from read_later_opds.store import Store, generate_secret

SECRET_RE = re.compile(r"^[a-z]{3,7}(-[a-z]{3,7}){2}$")


def test_secret_format():
    for _ in range(50):
        assert SECRET_RE.match(generate_secret())


def test_create_and_get(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    secret = store.create_user("token-abc")
    assert SECRET_RE.match(secret)
    assert store.get_token(secret) == "token-abc"
    assert store.get_token("maple-crater-nine") in (None, "token-abc")


def test_unknown_secret(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    assert store.get_token("nope-nope-nope") is None


def test_regenerate(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    secret = store.create_user("token-abc")
    new_secret = store.regenerate_secret(secret)
    assert new_secret != secret
    assert store.get_token(secret) is None
    assert store.get_token(new_secret) == "token-abc"
    assert store.regenerate_secret("unknown-unknown-unknown") is None


def test_delete(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    secret = store.create_user("token-abc")
    assert store.delete_user(secret) is True
    assert store.get_token(secret) is None
    assert store.delete_user(secret) is False


def test_stripe_ref_reuse(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    store.create_user("token-1", stripe_ref="cs_test_123")
    assert store.stripe_ref_used("cs_test_123") is True
    assert store.stripe_ref_used("cs_test_456") is False
    with pytest.raises(ValueError):
        store.create_user("token-2", stripe_ref="cs_test_123")
