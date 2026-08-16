import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# A stored entry is checked before it is served. The cache is an optimisation
# for stability, never a source of truth: because generation is deterministic,
# anything that looks wrong can be discarded and rebuilt for free.
_ZIP_MAGIC = b"PK\x03\x04"
_MIMETYPE_OFFSET = 30
_MIMETYPE = b"mimetype"


def cache_key(build_version: int, user: str, connector: str, article_id: str) -> str:
    """Filename for one cached EPUB.

    Hashed rather than composed into a path for three reasons: the article id
    is chosen upstream and must never reach the filesystem, tenants must not
    collide, and in multi-tenant mode `user` is the catalog secret, which
    should not sit on disk in plaintext.
    """
    raw = f"{build_version}:{user}:{connector}:{article_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _looks_like_epub(data: bytes) -> bool:
    return (
        data.startswith(_ZIP_MAGIC)
        and data[_MIMETYPE_OFFSET : _MIMETYPE_OFFSET + len(_MIMETYPE)] == _MIMETYPE
    )


class EpubCache:
    """The disabled cache: every deployment gets this unless it opts in."""

    def get(self, key: str) -> bytes | None:
        return None

    def put(self, key: str, data: bytes) -> None:
        return None


class DiskEpubCache(EpubCache):
    """EPUBs on disk, bounded by total size, evicted least-recently-used first.

    Failure is never fatal. A download that cannot be cached is still a
    download, so every filesystem error here degrades to a miss rather than
    propagating to the request.
    """

    def __init__(self, directory: str, max_bytes: int):
        self.dir = Path(directory)
        self.max_bytes = max_bytes
        self.dir.mkdir(parents=True, exist_ok=True)
        # 0700 explicitly rather than via mkdir's mode, which the umask masks
        # and which does nothing for a directory that already existed. This
        # holds the user's reading material — a stronger claim on the
        # filesystem than anything else the app stores.
        os.chmod(self.dir, 0o700)

    def get(self, key: str) -> bytes | None:
        path = self.dir / key
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not _looks_like_epub(data):
            logger.warning("discarding a cache entry that is not an EPUB: %s", key)
            self._unlink(path)
            return None
        try:
            # Touch for LRU ordering; mtime is the only recency record kept.
            os.utime(path)
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes) -> None:
        try:
            # Written to a temp file and moved into place, so a reader never
            # sees a partial EPUB and a crash mid-write leaves nothing corrupt.
            # Two devices racing on the same article both write, which is
            # harmless: generation is deterministic, so the bytes are identical.
            fd, tmp = tempfile.mkstemp(dir=self.dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, self.dir / key)
            except OSError:
                self._unlink(Path(tmp))
                raise
        except OSError:
            logger.warning("could not cache %s; serving without storing", key, exc_info=True)
            return
        self._evict()

    def _evict(self) -> None:
        try:
            entries = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.dir.iterdir()]
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        for _, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            if self._unlink(path):
                total -= size

    def _unlink(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def build_cache(directory: str | None, max_bytes: int) -> EpubCache:
    """The configured cache, or the disabled one.

    Both an unset directory and a zero cap mean off, matching how the rate
    limits in config.py read 0 as "turn it off".
    """
    if not directory or max_bytes <= 0:
        return EpubCache()
    try:
        return DiskEpubCache(directory, max_bytes)
    except OSError:
        # An unusable cache directory is a misconfiguration, not a reason to
        # refuse to serve books.
        logger.warning("EPUB cache directory %s is unusable; caching is off", directory, exc_info=True)
        return EpubCache()
