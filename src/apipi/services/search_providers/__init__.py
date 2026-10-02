import httpx

from apipi.services.search_providers.base import (
    KeySource,
    SearchHit,
    SearchOptions,
    SearchProvider,
    SearchProviderError,
    SearchQuery,
    SearchResponse,
    SearchTarget,
)
from apipi.services.search_providers.staan import StaanProvider
from apipi.services.search_providers.tavily import TavilyProvider

PROVIDERS: dict[str, type[TavilyProvider] | type[StaanProvider]] = {
    "tavily": TavilyProvider,
    "staan": StaanProvider,
}


def build_provider(target: SearchTarget, client: httpx.AsyncClient) -> SearchProvider:
    factory = PROVIDERS.get(target.provider)
    if factory is None:
        raise SearchProviderError(
            "search_unavailable", "the search provider is not available"
        )
    return factory(target, client)


__all__ = [
    "PROVIDERS",
    "KeySource",
    "SearchHit",
    "SearchOptions",
    "SearchProvider",
    "SearchProviderError",
    "SearchQuery",
    "SearchResponse",
    "SearchTarget",
    "build_provider",
]
