import json
from typing import Any

import httpx
import pytest

from apipi.services.search_providers import (
    SearchOptions,
    SearchProviderError,
    SearchQuery,
    SearchResponse,
    SearchTarget,
    build_provider,
)


def _target(provider: str, **options: Any) -> SearchTarget:
    merged: dict[str, Any] = {
        "base_url": None,
        "timeout": 5.0,
        "max_results": 5,
        "tavily_depth": "basic",
        "staan_market": "en-us",
    }
    merged.update(options)
    return SearchTarget(
        provider=provider,
        options=SearchOptions(**merged),
        credential="secret-key",
        key_source="operator",
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


TAVILY_BODY = {
    "results": [
        {
            "title": "One",
            "url": "https://one.example/a",
            "content": "first snippet",
            "score": 0.9,
            "published_date": "2026-01-02",
        },
        {"title": "Two", "url": "https://two.example/b", "content": "second"},
    ],
    "usage": {"credits": 1},
}

STAAN_BODY = {
    "search_id": "s1",
    "web": {
        "results": [
            {
                "title": "One",
                "url": "https://one.example/a",
                "snippet": "first snippet",
                "hostname": "one.example",
            },
            {"title": "Two", "url": "https://two.example/b", "snippet": "second"},
        ]
    },
}


async def test_tavily_request_and_normalized_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TAVILY_BODY)

    async with _client(handler) as client:
        provider = build_provider(_target("tavily"), client)
        response = await provider.search(
            SearchQuery("pi agents", 5, ("example.com", "docs.example.org"))
        )
    request = seen[0]
    assert str(request.url) == "https://api.tavily.com/search"
    assert request.headers["authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body["query"] == "pi agents"
    assert body["max_results"] == 5
    assert body["search_depth"] == "basic"
    assert body["include_domains"] == ["example.com", "docs.example.org"]
    assert response.units == 1
    assert [
        (hit.title, hit.url, hit.snippet, hit.published_date)
        for hit in response.results
    ] == [
        ("One", "https://one.example/a", "first snippet", "2026-01-02"),
        ("Two", "https://two.example/b", "second", None),
    ]


async def test_tavily_omits_domains_when_empty_and_honors_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TAVILY_BODY)

    async with _client(handler) as client:
        provider = build_provider(
            _target("tavily", base_url="http://proxy.local:9000/"), client
        )
        await provider.search(SearchQuery("q", 3))
    assert str(seen[0].url) == "http://proxy.local:9000/search"
    assert "include_domains" not in json.loads(seen[0].content)


@pytest.mark.parametrize(
    ("depth", "body", "units"),
    [
        ("basic", {"results": []}, 1),
        ("advanced", {"results": []}, 2),
        ("basic", {"results": [], "usage": {"credits": 3}}, 3),
        ("advanced", {"results": [], "usage": {"credits": 1}}, 1),
        ("advanced", {"results": [], "usage": {"credits": "x"}}, 2),
        ("basic", {"results": [], "usage": {"credits": 0}}, 1),
    ],
)
async def test_tavily_units(depth: str, body: dict[str, Any], units: int) -> None:
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        provider = build_provider(_target("tavily", tavily_depth=depth), client)
        response = await provider.search(SearchQuery("q", 5))
    assert response.units == units


async def test_tavily_caps_results_to_request() -> None:
    body = {
        "results": [
            {"title": str(i), "url": f"https://x.example/{i}", "content": ""}
            for i in range(6)
        ]
    }
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        provider = build_provider(_target("tavily"), client)
        response = await provider.search(SearchQuery("q", 2))
    assert len(response.results) == 2


async def test_staan_request_and_normalized_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=STAAN_BODY)

    async with _client(handler) as client:
        provider = build_provider(_target("staan", staan_market="de-de"), client)
        response = await provider.search(SearchQuery("pi agents", 5, ("example.com",)))
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.staan.ai/v2/search/web"
    assert request.headers["authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body == {
        "q": "pi agents",
        "market": "de-de",
        "include_domains": ["example.com"],
    }
    assert response.units == 1
    assert [
        (hit.title, hit.url, hit.snippet, hit.published_date)
        for hit in response.results
    ] == [
        ("One", "https://one.example/a", "first snippet", None),
        ("Two", "https://two.example/b", "second", None),
    ]


async def test_staan_truncates_query_and_slices_results() -> None:
    seen: list[httpx.Request] = []
    body = {
        "web": {
            "results": [
                {"title": str(i), "url": f"https://x.example/{i}", "snippet": "s"}
                for i in range(10)
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        provider = build_provider(_target("staan"), client)
        response = await provider.search(SearchQuery("x" * 900, 3))
    sent = json.loads(seen[0].content)
    assert len(sent["q"]) == 400
    assert "include_domains" not in sent
    assert len(response.results) == 3
    assert response.units == 1


async def test_staan_rejects_more_than_ten_domains() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=STAAN_BODY)

    domains = tuple(f"d{i}.example" for i in range(11))
    async with _client(handler) as client:
        provider = build_provider(_target("staan"), client)
        with pytest.raises(SearchProviderError) as raised:
            await provider.search(SearchQuery("q", 5, domains))
    assert raised.value.code == "invalid_request"
    assert raised.value.charged is False
    assert calls == []


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_both_providers_return_the_same_shape(provider: str) -> None:
    body = TAVILY_BODY if provider == "tavily" else STAAN_BODY
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        response = await build_provider(_target(provider), client).search(
            SearchQuery("q", 5)
        )
    assert isinstance(response, SearchResponse)
    assert [hit.url for hit in response.results] == [
        "https://one.example/a",
        "https://two.example/b",
    ]
    assert all(
        isinstance(hit.title, str) and isinstance(hit.snippet, str)
        for hit in response.results
    )


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_redirects_are_not_followed(provider: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "evil.example" in str(request.url):
            return httpx.Response(200, json={})
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    async with _client(handler) as client:
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(_target(provider), client).search(SearchQuery("q", 5))
    assert raised.value.code == "search_failed"
    assert raised.value.charged is False
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_timeout_is_not_charged(provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as client:
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(_target(provider), client).search(SearchQuery("q", 5))
    assert raised.value.code == "search_timeout"
    assert raised.value.charged is False


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_slow_provider_hits_total_timeout(provider: str) -> None:
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        target = _target(provider, timeout=0.05)
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(target, client).search(SearchQuery("q", 5))
    assert raised.value.code == "search_timeout"
    assert raised.value.charged is False


@pytest.mark.parametrize("provider", ["tavily", "staan"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "search_failed"),
        (401, "search_failed"),
        (429, "search_unavailable"),
        (500, "search_unavailable"),
        (503, "search_unavailable"),
    ],
)
async def test_http_errors_are_not_charged(
    provider: str, status: int, code: str
) -> None:
    async with _client(lambda request: httpx.Response(status, text="boom")) as client:
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(_target(provider), client).search(SearchQuery("q", 5))
    assert raised.value.code == code
    assert raised.value.charged is False
    assert "boom" not in raised.value.message
    assert "secret-key" not in raised.value.message


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_transport_error_is_not_charged(provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with _client(handler) as client:
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(_target(provider), client).search(SearchQuery("q", 5))
    assert raised.value.code == "search_unavailable"
    assert raised.value.charged is False


@pytest.mark.parametrize("provider", ["tavily", "staan"])
async def test_invalid_json_after_200_is_charged(provider: str) -> None:
    async with _client(lambda request: httpx.Response(200, text="not json")) as client:
        with pytest.raises(SearchProviderError) as raised:
            await build_provider(_target(provider), client).search(SearchQuery("q", 5))
    assert raised.value.code == "search_failed"
    assert raised.value.charged is True


async def test_unknown_provider_error_hides_name() -> None:
    async with _client(lambda request: httpx.Response(200, json={})) as client:
        with pytest.raises(SearchProviderError) as raised:
            build_provider(_target("nope"), client)
    assert "nope" not in raised.value.message


def test_target_repr_hides_credential() -> None:
    assert "secret-key" not in repr(_target("tavily"))
