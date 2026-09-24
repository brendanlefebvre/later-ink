from datetime import datetime

from later_ink.connectors.base import parse_epoch


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
