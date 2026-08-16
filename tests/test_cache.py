import io
import zipfile

from later_ink.cache import DiskEpubCache, EpubCache, build_cache, cache_key


def _epub_bytes(payload: bytes = b"<html/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        z.writestr(mt, b"application/epub+zip")
        z.writestr("EPUB/x.xhtml", payload)
    return buf.getvalue()


def test_disabled_cache_is_the_default(tmp_path):
    cache = build_cache(None, 1024)
    assert isinstance(cache, EpubCache)
    assert not isinstance(cache, DiskEpubCache)
    cache.put("k", _epub_bytes())
    assert cache.get("k") is None
    assert list(tmp_path.iterdir()) == []


def test_zero_cap_disables_the_cache(tmp_path):
    assert not isinstance(build_cache(str(tmp_path), 0), DiskEpubCache)


def test_put_then_get_round_trips(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    data = _epub_bytes()
    cache.put("k", data)
    assert cache.get("k") == data


def test_miss_returns_none(tmp_path):
    assert DiskEpubCache(str(tmp_path), 1024 * 1024).get("nope") is None


def test_corrupt_entry_is_a_miss_and_is_removed(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    cache.put("k", _epub_bytes())
    (tmp_path / "k").write_bytes(b"not an epub at all")
    assert cache.get("k") is None
    assert not (tmp_path / "k").exists()


def test_eviction_drops_oldest_until_under_cap(tmp_path):
    import os

    big = _epub_bytes(b"x" * 4096)
    cache = DiskEpubCache(str(tmp_path), len(big) * 2)
    for name, age in (("old", 1000), ("mid", 2000), ("new", 3000)):
        cache.put(name, big)
        os.utime(tmp_path / name, (age, age))
    cache.put("newest", big)
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total <= len(big) * 2
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "newest").exists()


def test_cache_directory_is_private(tmp_path):
    target = tmp_path / "cache"
    DiskEpubCache(str(target), 1024)
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_key_changes_with_every_component():
    base = cache_key(1, "local", "readwise", "a1")
    assert base != cache_key(2, "local", "readwise", "a1")
    assert base != cache_key(1, "other", "readwise", "a1")
    assert base != cache_key(1, "local", "wallabag", "a1")
    assert base != cache_key(1, "local", "readwise", "a2")


def test_key_is_a_bare_hex_digest(tmp_path):
    # The article id comes from upstream, so it must never reach a filesystem
    # path. Hashing the whole composed key also keeps a tenant secret off disk.
    key = cache_key(1, "sec/../../etc", "readwise", "../../evil")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_write_failure_does_not_raise(tmp_path):
    cache = DiskEpubCache(str(tmp_path), 1024 * 1024)
    tmp_path.chmod(0o500)
    try:
        cache.put("k", _epub_bytes())  # must not raise
    finally:
        tmp_path.chmod(0o700)
    assert cache.get("k") is None
