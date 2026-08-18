import io
import os
import zipfile
from pathlib import Path

import pytest

from later_ink.cache import DiskEpubCache, EpubCache, build_cache, cache_key


def _epub_bytes(payload: bytes = b"<html/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        z.writestr(mt, b"application/epub+zip")
        z.writestr("EPUB/x.xhtml", payload)
    return buf.getvalue()


def _k(label: str) -> str:
    """A realistic entry name, since eviction only considers names it wrote."""
    return cache_key(1, "local", "readwise", label)


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
    big = _epub_bytes(b"x" * 4096)
    cache = DiskEpubCache(str(tmp_path), len(big) * 2)
    for label, age in (("old", 1000), ("mid", 2000), ("new", 3000)):
        cache.put(_k(label), big)
        os.utime(tmp_path / _k(label), (age, age))
    cache.put(_k("newest"), big)
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total <= len(big) * 2
    assert not (tmp_path / _k("old")).exists()
    assert (tmp_path / _k("newest")).exists()


def test_eviction_skips_an_entry_that_vanishes_during_the_scan(tmp_path, monkeypatch):
    # A cache directory can be shared by several app workers. One entry
    # ("b") is made to really disappear mid-scan, between DiskEpubCache
    # listing the directory and stat'ing that particular file — exactly what
    # a peer worker's own concurrent eviction or overwrite would do. The
    # deletion is genuine (a real unlink), not a stubbed stat() return value:
    # monkeypatch only controls *when* it happens, not what stat() reports.
    for label, size, age in (("a", 10, 1000), ("b", 10, 2000), ("c", 10, 3000), ("d", 10, 4000)):
        (tmp_path / _k(label)).write_bytes(b"x" * size)
        os.utime(tmp_path / _k(label), (age, age))

    real_stat = Path.stat
    triggered = {"done": False}

    def flaky_stat(self, *args, **kwargs):
        if self.name == _k("b") and not triggered["done"]:
            triggered["done"] = True
            (tmp_path / _k("b")).unlink()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    cache = DiskEpubCache(str(tmp_path), 25)
    cache._evict()  # must not raise, and must still evict "a"

    assert triggered["done"]
    assert not (tmp_path / _k("a")).exists()  # oldest survivor, evicted normally
    assert not (tmp_path / _k("b")).exists()  # vanished mid-scan, via the race
    assert (tmp_path / _k("c")).exists()
    assert (tmp_path / _k("d")).exists()
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total <= 25


def test_eviction_never_deletes_a_file_the_cache_did_not_write(tmp_path):
    # EPUB_CACHE_DIR=/data is a plausible thing for a self-hoster to type: the
    # docs say to put the cache on the Docker volume, and /data is that
    # volume — which is also where app.db lives. Eviction walks the whole
    # directory by mtime, so without this it would delete the database (and
    # in multi-tenant mode every user and encrypted token in it) to make room
    # for a book.
    stranger = tmp_path / "app.db"
    stranger.write_bytes(b"y" * 8192)
    subdir = tmp_path / "backups"
    subdir.mkdir()

    big = _epub_bytes(b"x" * 4096)
    cache = DiskEpubCache(str(tmp_path), len(big))
    cache.put(_k("a1"), big)
    cache.put(_k("a2"), big)  # takes the total over the cap, forcing eviction

    assert stranger.read_bytes() == b"y" * 8192
    assert subdir.is_dir()
    assert not (tmp_path / _k("a1")).exists()  # the cache still evicts its own


def test_a_temp_file_orphaned_by_a_crash_is_reclaimable(tmp_path, monkeypatch):
    # An entry is written to a temp file and moved into place. A crash in
    # between leaves that temp file behind, counting against the cap forever
    # unless eviction recognises the name as the cache's own.
    class Crash(Exception):
        pass

    def crashing_replace(src, dst):
        raise Crash

    monkeypatch.setattr("later_ink.cache.os.replace", crashing_replace)
    cache = DiskEpubCache(str(tmp_path), 1024)
    with pytest.raises(Crash):
        cache.put(_k("a1"), _epub_bytes(b"x" * 512))
    monkeypatch.undo()

    orphans = list(tmp_path.iterdir())
    assert len(orphans) == 1
    assert orphans[0].name.startswith(".epub-tmp-")

    DiskEpubCache(str(tmp_path), 1)._evict()
    assert list(tmp_path.iterdir()) == []


def test_the_database_directory_is_refused_as_a_cache_directory(tmp_path):
    # Defence in depth behind the name filter: a cache pointed at the volume
    # holding app.db would also narrow that directory to 0700. Nothing about
    # the deployment needs the two to share a directory, so refuse rather
    # than take it over.
    # Spelled with a ".." so the comparison is on resolved paths, not strings.
    refused = build_cache(str(tmp_path / "epubs" / ".."), 1024, reserved_dir=str(tmp_path))
    assert not isinstance(refused, DiskEpubCache)
    ok = build_cache(str(tmp_path / "epubs"), 1024, reserved_dir=str(tmp_path))
    assert isinstance(ok, DiskEpubCache)


def test_a_symlink_loop_disables_the_cache_rather_than_raising(tmp_path):
    # A cache directory that cannot be resolved has to leave the app serving
    # books, like every other misconfiguration here — build_cache is called
    # from lifespan, so anything escaping it stops the container from starting.
    #
    # Easy to get wrong: through Python 3.12 — the version the image pins —
    # Path.resolve() reports a symlink loop as RuntimeError, which is not an
    # OSError and which an OSError-only guard therefore lets through. On 3.13+
    # resolve() gives up loosely and the same directory fails later inside
    # DiskEpubCache, so this test only has teeth on the runtime that ships.
    loop, other = tmp_path / "loop", tmp_path / "other"
    loop.symlink_to(other)
    other.symlink_to(loop)
    cache = build_cache(str(loop), 1024 * 1024, reserved_dir=str(tmp_path / "db"))
    assert not isinstance(cache, DiskEpubCache)
    assert cache.get("k") is None


def test_a_cache_that_was_never_asked_for_is_not_misconfigured(tmp_path):
    # The default. Nothing to report: not opting in is not a mistake.
    assert build_cache(None, 1024).misconfigured is None
    assert build_cache(str(tmp_path), 0).misconfigured is None


def test_a_cache_that_was_asked_for_and_refused_reports_why(tmp_path):
    # Asked for and not running is the case worth surfacing — /healthz turns it
    # into an unhealthy container, because nothing else about the deployment
    # looks wrong when caching quietly does not happen.
    collided = build_cache(str(tmp_path), 1024, reserved_dir=str(tmp_path))
    assert collided.misconfigured
    assert not collided.enabled

    loop, other = tmp_path / "loop", tmp_path / "other"
    loop.symlink_to(other)
    other.symlink_to(loop)
    unusable = build_cache(str(loop), 1024, reserved_dir=str(tmp_path / "db"))
    assert unusable.misconfigured
    assert not unusable.enabled


def test_the_reported_reason_never_echoes_the_configured_path(tmp_path):
    # /healthz is unauthenticated and documents that it leaks no config, so the
    # message names the variable rather than its value. The operator set the
    # path and does not need telling; a stranger probing the endpoint does.
    secret = tmp_path / "srv" / "brendans-private-volume"
    secret.mkdir(parents=True)
    reason = build_cache(str(secret), 1024, reserved_dir=str(secret)).misconfigured
    assert "EPUB_CACHE_DIR" in reason
    assert str(secret) not in reason
    assert "brendans-private-volume" not in reason


def test_a_working_cache_reports_nothing(tmp_path):
    cache = build_cache(str(tmp_path / "epubs"), 1024, reserved_dir=str(tmp_path / "db"))
    assert isinstance(cache, DiskEpubCache)
    assert cache.enabled
    assert cache.misconfigured is None


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
