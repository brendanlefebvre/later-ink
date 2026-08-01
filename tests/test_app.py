import re

import pytest
from fastapi.testclient import TestClient

from read_later_opds import main
from read_later_opds.connectors import readwise
from read_later_opds.connectors.base import Article, Folder
from read_later_opds.connectors.readwise import ReadwiseConnector

SECRET_PAT = r"([a-z]+(?:-[a-z]+){3})"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)

    async def fake_validate(token):
        return token == "good-token"

    monkeypatch.setattr(readwise, "validate_token", fake_validate)

    async def fake_list_folders(self):
        return [Folder("later", "Read Later")]

    async def fake_list_articles(self, folder_id, cursor=None):
        return [Article(id="42", title="Test Article", author="Ann Author")], None

    monkeypatch.setattr(ReadwiseConnector, "list_folders", fake_list_folders)
    monkeypatch.setattr(ReadwiseConnector, "list_articles", fake_list_articles)

    with TestClient(app=main.app) as c:
        yield c


def _signup(client) -> tuple[str, str]:
    resp = client.post("/start", data={"readwise_token": "good-token"})
    assert resp.status_code == 200
    m = re.search(rf"/{SECRET_PAT}/regenerate", resp.text)
    assert m, "no catalog secret in success page"
    secret = m.group(1)
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', resp.text)
    assert m, "no csrf token in success page"
    return secret, m.group(1)


def test_landing(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "OPDS" in resp.text


def test_health(client):
    resp = client.get("/health")
    assert resp.json()["signup"] == "free"


def test_signup_bad_token(client):
    resp = client.post("/start", data={"readwise_token": "bad-token"})
    assert resp.status_code == 400
    assert "rejected" in resp.text


def test_signup_and_browse(client):
    secret, _ = _signup(client)

    resp = client.get(f"/{secret}/")
    assert resp.status_code == 200
    assert "kind=navigation" in resp.headers["content-type"]
    assert "Read Later" in resp.text

    resp = client.get(f"/{secret}/later/")
    assert resp.status_code == 200
    assert "Test Article" in resp.text
    assert f"/{secret}/articles/42.epub" in resp.text


def test_unknown_secret_404(client):
    resp = client.get("/maple-crater-nothing-here/")
    assert resp.status_code == 404


def test_reserved_path_not_a_secret(client):
    resp = client.get("/start")
    assert resp.status_code == 200  # onboarding page, not a 404-catalog probe


def test_rate_limit_unknown_secrets(client):
    for _ in range(20):
        resp = client.get("/guess-guess-guess-guess/")
        assert resp.status_code == 404
    resp = client.get("/guess-guess-guess-guess/")
    assert resp.status_code == 429


def test_csrf_required(client):
    secret, _ = _signup(client)
    assert client.post(f"/{secret}/regenerate").status_code == 403
    assert client.post(f"/{secret}/delete", data={"csrf": "wrong"}).status_code == 403
    assert client.get(f"/{secret}/").status_code == 200  # still alive


def test_regenerate_and_delete(client):
    secret, csrf = _signup(client)

    resp = client.post(f"/{secret}/regenerate", data={"csrf": csrf})
    assert resp.status_code == 200
    m = re.search(rf"/{SECRET_PAT}/regenerate", resp.text)
    new_secret = m.group(1)
    assert new_secret != secret
    new_csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', resp.text).group(1)

    assert client.get(f"/{secret}/").status_code == 404
    assert client.get(f"/{new_secret}/").status_code == 200

    resp = client.post(f"/{new_secret}/delete", data={"csrf": new_csrf})
    assert resp.status_code == 200
    assert client.get(f"/{new_secret}/").status_code == 404


def test_upstream_error_becomes_502(client):
    from read_later_opds.connectors.base import UpstreamError

    async def failing_list_articles(self, folder_id, cursor=None):
        raise UpstreamError("Readwise is rate-limiting this account", 429)

    secret, _ = _signup(client)
    orig = ReadwiseConnector.list_articles
    ReadwiseConnector.list_articles = failing_list_articles
    try:
        resp = client.get(f"/{secret}/later/")
    finally:
        ReadwiseConnector.list_articles = orig
    assert resp.status_code == 502
    assert "rate-limiting" in resp.text


def test_single_user_mode_root(client):
    resp = client.get("/opds/")
    assert resp.status_code == 200
    assert "kind=navigation" in resp.headers["content-type"]
