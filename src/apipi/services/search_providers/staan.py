from typing import Any

import httpx

from apipi.services.search_providers.base import (
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    SearchHit,
    SearchProviderError,
    SearchQuery,
    SearchResponse,
    SearchTarget,
    clip,
    post_json,
)

DEFAULT_BASE_URL = "https://api.staan.ai"
MAX_QUERY_CHARS = 400
MAX_DOMAINS = 10


class StaanProvider:
    def __init__(self, target: SearchTarget, client: httpx.AsyncClient) -> None:
        self._target = target
        self._client = client
        base = target.options.base_url or DEFAULT_BASE_URL
        self._url = base.rstrip("/") + "/v2/search/web"

    async def search(self, query: SearchQuery) -> SearchResponse:
        options = self._target.options
        if len(query.allowed_domains) > MAX_DOMAINS:
            raise SearchProviderError(
                "invalid_request",
                f"at most {MAX_DOMAINS} allowed domains are supported",
            )
        body: dict[str, Any] = {
            "q": query.query[:MAX_QUERY_CHARS],
            "market": options.staan_market,
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
        web = data.get("web")
        raw = web.get("results") if isinstance(web, dict) else None
        hits: list[SearchHit] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = item.get("title")
            snippet = item.get("snippet")
            hits.append(
                SearchHit(
                    title=clip(
                        title if isinstance(title, str) else "", MAX_TITLE_CHARS
                    ),
                    url=url,
                    snippet=clip(
                        snippet if isinstance(snippet, str) else "", MAX_SNIPPET_CHARS
                    ),
                )
            )
        return SearchResponse(results=hits[: query.max_results], units=1)
