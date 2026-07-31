import httpx

from .base import Article, Connector, Folder

BASE_URL = "https://readwise.io/api/v3"

LOCATIONS = [
    Folder("later", "Read Later", "Articles saved for later"),
    Folder("new", "New", "Recently added articles"),
    Folder("shortlist", "Shortlist", "Shortlisted articles"),
    Folder("archive", "Archive", "Archived articles"),
    Folder("feed", "Feed", "Feed articles"),
]


class ReadwiseConnector(Connector):
    name = "readwise"
    description = "Readwise Reader"

    def __init__(self, token: str):
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Token {token}"},
            timeout=30.0,
        )

    async def list_folders(self) -> list[Folder]:
        return LOCATIONS

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        params: dict[str, str] = {
            "location": folder_id,
            "category": "article",
        }
        if cursor:
            params["pageCursor"] = cursor

        resp = await self._client.get("/list/", params=params)
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for doc in data.get("results", []):
            articles.append(
                Article(
                    id=str(doc["id"]),
                    title=doc.get("title") or "Untitled",
                    author=doc.get("author"),
                    summary=doc.get("summary"),
                    url=doc.get("source_url") or doc.get("url"),
                    word_count=doc.get("word_count"),
                )
            )

        next_cursor = data.get("nextPageCursor")
        return articles, next_cursor

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        resp = await self._client.get(
            "/list/",
            params={"id": article_id, "withHtmlContent": "true"},
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            raise ValueError(f"Article {article_id} not found")

        doc = results[0]
        article = Article(
            id=str(doc["id"]),
            title=doc.get("title") or "Untitled",
            author=doc.get("author"),
            summary=doc.get("summary"),
            url=doc.get("source_url") or doc.get("url"),
            word_count=doc.get("word_count"),
        )

        html_content = doc.get("html_content", "")
        if not html_content:
            raise ValueError(f"No HTML content for article {article_id}")

        return article, html_content

    async def close(self):
        await self._client.aclose()
