import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

KeySource = Literal["operator", "tenant"]

MAX_SNIPPET_CHARS = 1500
MAX_TITLE_CHARS = 300


@dataclass(frozen=True)
class SearchOptions:
    base_url: str | None
    timeout: float
    max_results: int
    tavily_depth: str = "basic"
    staan_market: str = "en-us"


@dataclass(frozen=True)
class SearchTarget:
    provider: str
    options: SearchOptions
    credential: str = field(repr=False)
    key_source: KeySource


@dataclass(frozen=True)
class SearchQuery:
    query: str
    max_results: int
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    published_date: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchHit]
    units: int


class SearchProviderError(Exception):
    """A provider call failed.

    `code` is one of `search_unavailable`, `search_timeout`,
    `search_failed` or `invalid_request`. `charged` is true only when the
    provider billed the call anyway; `units` is then what it billed.
    """

    def __init__(
        self, code: str, message: str, *, charged: bool = False, units: int = 1
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.charged = charged
        self.units = units


class SearchProvider(Protocol):
    async def search(self, query: SearchQuery) -> SearchResponse: ...


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(timeout):
            response = await client.post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise SearchProviderError(
            "search_timeout", "the search provider timed out"
        ) from exc
    except httpx.HTTPError as exc:
        raise SearchProviderError(
            "search_unavailable", "the search provider is not reachable"
        ) from exc
    status = response.status_code
    if 300 <= status < 400:
        raise SearchProviderError(
            "search_failed", "the search provider answered with a redirect"
        )
    if status == 429 or status >= 500:
        raise SearchProviderError(
            "search_unavailable", f"the search provider answered with HTTP {status}"
        )
    if status >= 400:
        raise SearchProviderError(
            "search_failed", f"the search provider rejected the request (HTTP {status})"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise SearchProviderError(
            "search_failed",
            "the search provider answered with invalid JSON",
            charged=True,
        ) from exc
    if not isinstance(data, dict):
        raise SearchProviderError(
            "search_failed",
            "the search provider answered with an unexpected body",
            charged=True,
        )
    return data
