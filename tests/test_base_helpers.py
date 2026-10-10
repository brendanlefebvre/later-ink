import asyncio
from datetime import datetime

from later_ink.connectors.base import Article, Connector, Folder, parse_epoch


def test_parse_epoch_returns_naive_utc():
    # 1735779845 == 2025-01-02T01:04:05Z
    result = parse_epoch(1735779845)
    assert result == datetime(2025, 1, 2, 1, 4, 5)
    assert result.tzinfo is None


def test_parse_epoch_accepts_numeric_string():
    assert parse_epoch("1735779845") == datetime(2025, 1, 2, 1, 4, 5)


def test_parse_epoch_none_and_blank_and_garbage_return_none():
    assert parse_epoch(None) is None
    assert parse_epoch("") is None
    assert parse_epoch("not-a-number") is None


class _BareConnector(Connector):
    """A connector that does NOT override close(), to prove the ABC default."""

    name = "bare"
    description = "Bare"

    async def list_folders(self) -> list[Folder]:
        return []

    async def list_articles(self, folder_id, cursor=None):
        return [], None

    async def get_article_html(self, article_id):
        return Article(id=article_id, title="x"), "<p>x</p>"


def test_connector_close_default_is_a_noop_awaitable():
    async def go():
        conn = _BareConnector()
        assert await conn.close() is None
        await conn.close()  # idempotent

    asyncio.run(go())
