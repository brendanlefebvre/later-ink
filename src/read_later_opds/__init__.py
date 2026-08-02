"""Later.Ink — read-it-later queues served as OPDS catalogs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("read-later-opds")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0+source"
