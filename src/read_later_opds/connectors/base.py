from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class UpstreamError(Exception):
    """The upstream read-it-later service failed; surface a readable message."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Folder:
    id: str
    title: str
    description: str = ""


@dataclass
class Article:
    id: str
    title: str
    author: str | None = None
    summary: str | None = None
    url: str | None = None
    updated: datetime = field(default_factory=datetime.now)
    word_count: int | None = None
    language: str | None = None


class Connector(ABC):
    name: str
    description: str

    @abstractmethod
    async def list_folders(self) -> list[Folder]:
        ...

    @abstractmethod
    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Return (articles, next_cursor). next_cursor is None when no more pages."""
        ...

    @abstractmethod
    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        """Return (article_metadata, html_content)."""
        ...
