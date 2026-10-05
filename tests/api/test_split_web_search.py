import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx import AsyncClient
from tests.support.http import auth, tenant_of
from tests.support.search import settings
from tests.support.split_worker import split_client_for, wait_for_idle

from apipi.config import Settings
from apipi.services.search import SearchService
from apipi.store.engine import Store
from apipi.store.repo import search_usage_for_turn
from apipi.worker.fake_harness import FakeHarness

__all__ = ["settings"]

TAVILY_BODY = {
    "results": [
        {
            "title": "Pi",
            "url": "https://pi.example/docs",
            "content": "Pi is a small agent loop",
            "published_date": "2026-01-02",
        }
    ],
    "usage": {"credits": 1},
}


class SearchingHarness(FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.flags: list[Any] = []
        self.turn_ids: list[str] = []
        self.replies: list[dict[str, Any]] = []

    async def generate(
        self, text: str, **kwargs: Any
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.flags.append(kwargs.get("web_search"))
        hook = kwargs.get("search")
        self.turn_ids.append(str(kwargs.get("turn_id")))
        if kwargs.get("web_search") is True and callable(hook):
            self.replies.append(
                await hook(
                    str(kwargs["session_id"]), str(kwargs["turn_id"]), "pi agents", 3
                )
            )
        async for item in super().generate(text, **kwargs):
            yield item


async def _run_turn(
    client: AsyncClient, token: str, tools: list[dict[str, Any]]
) -> uuid.UUID:
    agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={"name": "bot", "model": "test", "tools": tools},
    )
    assert agent.status_code == 200, agent.text
    session = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert session.status_code == 200, session.text
    session_id = session.json()["id"]
    posted = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "search please"},
    )
    assert posted.status_code == 200, posted.text
    await wait_for_idle(client, token, session_id)
    return uuid.UUID(session_id)


async def test_search_goes_worker_to_api_to_provider_and_is_counted(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    token = "split-search"
    harness = SearchingHarness()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TAVILY_BODY)

    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        app.state.search = SearchService(
            store,
            app.state.settings,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        session_id = await _run_turn(client, token, [{"type": "web_search"}])
        assert harness.flags == [True]
        reply = harness.replies[0]
        assert reply["ok"] is True
        assert reply["results"][0]["url"] == "https://pi.example/docs"
        assert len(seen) == 1
        assert seen[0].headers["authorization"] == "Bearer secret-key"
        assert "secret-key" not in str(reply)
        tenant_id = tenant_of(token)
        usage = await client.get(
            "/v1/apipi/usage",
            headers=auth(token),
            params={"session_id": str(session_id)},
        )
        assert usage.status_code == 200, usage.text
        assert usage.json()["search_calls"] == 1
        assert usage.json()["search_units"] == 1
        async with store.session() as db:
            calls, units, counts = await search_usage_for_turn(
                db, tenant_id, uuid.UUID(harness.turn_ids[0])
            )
        assert (calls, units) == (1, 1)
        assert counts == {"tavily/operator": {"calls": 1, "units": 1}}


async def test_agent_without_the_tool_never_gets_the_flag(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = SearchingHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        await _run_turn(client, "split-nosearch", [])
        assert harness.flags == [None]
        assert harness.replies == []
