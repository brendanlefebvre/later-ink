import re
import sqlite3

import pytest
from cryptography.fernet import Fernet

from read_later_opds.store import Store, generate_secret

SECRET_RE = re.compile(r"^[a-z]{3,7}(-[a-z]{3,7}){3}$")


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "test.db"), Fernet(Fernet.generate_key()))


def test_secret_format():
    for _ in range(50):
        assert SECRET_RE.match(generate_secret())


def test_create_and_get(store):
    secret = store.create_user("token-abc")
    assert SECRET_RE.match(secret)
    assert store.get_token(secret) == "token-abc"


def test_token_encrypted_at_rest(store):
    secret = store.create_user("token-abc")
    with sqlite3.connect(store.path) as conn:
        raw = conn.execute(
            "SELECT readwise_token FROM users WHERE secret = ?", (secret,)
        ).fetchone()[0]
    assert "token-abc" not in raw


def test_wrong_key_yields_none(tmp_path):
    path = str(tmp_path / "test.db")
    secret = Store(path, Fernet(Fernet.generate_key())).create_user("token-abc")
    other = Store(path, Fernet(Fernet.generate_key()))
    assert other.get_token(secret) is None


def test_unknown_secret(store):
    assert store.get_token("nope-nope-nope-nope") is None


def test_regenerate(store):
    secret = store.create_user("token-abc")
    new_secret = store.regenerate_secret(secret)
    assert new_secret != secret
    assert store.get_token(secret) is None
    assert store.get_token(new_secret) == "token-abc"
    assert store.regenerate_secret("unknown-unknown-unknown-unknown") is None


def test_delete(store):
    secret = store.create_user("token-abc")
    assert store.delete_user(secret) is True
    assert store.get_token(secret) is None
    assert store.delete_user(secret) is False


def test_stripe_ref_reuse(store):
    store.create_user("token-1", stripe_ref="cs_test_123")
    assert store.stripe_ref_used("cs_test_123") is True
    assert store.stripe_ref_used("cs_test_456") is False
    with pytest.raises(ValueError):
        store.create_user("token-2", stripe_ref="cs_test_123")


def test_miss_counter(store):
    assert store.miss_count("1.2.3.4", 3600) == 0
    for _ in range(5):
        store.record_miss("1.2.3.4", 3600)
    assert store.miss_count("1.2.3.4", 3600) == 5
    assert store.miss_count("5.6.7.8", 3600) == 0
