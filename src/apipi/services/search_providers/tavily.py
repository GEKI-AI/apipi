import math
from typing import Any

import httpx

from apipi.services.search_providers.base import (
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    SearchHit,
    SearchQuery,
    SearchResponse,
    SearchTarget,
    clip,
    post_json,
)

DEFAULT_BASE_URL = "https://api.tavily.com"
MAX_RESULTS = 20


class TavilyProvider:
    def __init__(self, target: SearchTarget, client: httpx.AsyncClient) -> None:
        self._target = target
        self._client = client
        base = target.options.base_url or DEFAULT_BASE_URL
        self._url = base.rstrip("/") + "/search"

    async def search(self, query: SearchQuery) -> SearchResponse:
        options = self._target.options
        body: dict[str, Any] = {
            "query": query.query,
            "max_results": min(max(query.max_results, 1), MAX_RESULTS),
            "search_depth": options.tavily_depth,
            "include_usage": True,
        }
        if query.allowed_domains:
            body["include_domains"] = list(query.allowed_domains)
        data = await post_json(
            self._client,
            self._url,
            headers={"Authorization": f"Bearer {self._target.credential}"},
            body=body,
            timeout=options.timeout,
        )
        raw = data.get("results")
        hits: list[SearchHit] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = item.get("title")
            content = item.get("content")
            published = item.get("published_date")
            hits.append(
                SearchHit(
                    title=clip(
                        title if isinstance(title, str) else "", MAX_TITLE_CHARS
                    ),
                    url=url,
                    snippet=clip(
                        content if isinstance(content, str) else "", MAX_SNIPPET_CHARS
                    ),
                    published_date=published
                    if isinstance(published, str) and published
                    else None,
                )
            )
        return SearchResponse(
            results=hits[: query.max_results],
            units=self._units(data, options.tavily_depth),
        )

    @staticmethod
    def _units(data: dict[str, Any], depth: str) -> int:
        usage = data.get("usage")
        if isinstance(usage, dict):
            credits = usage.get("credits")
            if (
                isinstance(credits, int | float)
                and not isinstance(credits, bool)
                and credits > 0
            ):
                return max(1, math.ceil(credits))
        return 2 if depth == "advanced" else 1
