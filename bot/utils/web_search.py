from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearxngSearch:
    """Small synchronous client for a SearXNG JSON endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 8.0,
        max_results: int = 5,
        max_snippet_chars: int = 700,
        language: str = "auto",
        user_agent: str = "sta-dc-bot/1.0",
        logger: logging.Logger | None = None,
    ) -> None:
        base_url = (base_url or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.path.rstrip("/").endswith("/search"):
            self.search_url = base_url
        else:
            self.search_url = f"{base_url}/search" if base_url else ""
        self.timeout = max(1.0, float(timeout))
        self.max_results = max(1, int(max_results))
        self.max_snippet_chars = max(80, int(max_snippet_chars))
        self.language = language.strip() or "auto"
        self.user_agent = user_agent.strip() or "sta-dc-bot/1.0"
        self.logger = logger or logging.getLogger(__name__)

    def search(self, query: str) -> list[SearchResult]:
        query = " ".join((query or "").split())[:300]
        if not self.search_url or not query:
            return []

        params = urlencode(
            {
                "q": query,
                "format": "json",
                "language": self.language,
                "safesearch": "1",
            }
        )
        request = Request(
            f"{self.search_url}?{params}",
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self.logger.warning("SearXNG search failed: %s", exc)
            return []

        results: list[SearchResult] = []
        for item in payload.get("results", [])[: self.max_results]:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            url = str(item.get("url") or item.get("link") or "").strip()
            snippet = " ".join(
                str(item.get("content") or item.get("snippet") or "").split()
            )
            if not title or not url:
                continue
            if len(snippet) > self.max_snippet_chars:
                snippet = snippet[: self.max_snippet_chars - 1].rstrip() + "…"
            results.append(SearchResult(title=title, url=url, snippet=snippet))

        return results

    @staticmethod
    def format_results(results: list[SearchResult], max_chars: int = 5000) -> str:
        if not results:
            return "（網路搜尋沒有找到可用結果。）"

        lines: list[str] = []
        used = 0
        for index, result in enumerate(results, start=1):
            line = f"[{index}] {result.title}\nURL: {result.url}\n摘要：{result.snippet}"
            if used + len(line) + 2 > max_chars:
                break
            lines.append(line)
            used += len(line) + 2
        return "\n\n".join(lines) or "（網路搜尋結果過長，沒有可用摘要。）"
