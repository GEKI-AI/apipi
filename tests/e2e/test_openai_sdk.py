from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store

pytestmark = pytest.mark.slow


@pytest.fixture
async def sdk_http(settings: Settings, store: Store) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_openai_sdk_subset(sdk_http: AsyncClient) -> None:
    openai = pytest.importorskip("openai")
    async_openai = getattr(openai, "AsyncOpenAI", None)
    if async_openai is None:
        pytest.skip("openai.AsyncOpenAI missing")
    sdk = async_openai(
        api_key="sdk",
        base_url="http://test/v1",
        http_client=sdk_http,
        max_retries=0,
    )
    agents = getattr(getattr(sdk, "beta", None), "agents", None)
    if agents is None:
        pytest.skip("openai SDK has no beta.agents")
    created = await agents.with_raw_response.create(
        model="test", name="bot", instructions="be brief"
    )
    assert created.status_code == 200
    agent = created.http_response.json()
    assert agent["name"] == "bot"
    assert agent["model"] == "test"
    sessions = agents.sessions
    session = await sessions.with_raw_response.create(
        environment={"type": "none"},
        agent_id=agent["id"],
        input="hello",
    )
    assert session.status_code == 200
    body = session.http_response.json()
    assert body["status"] == "idle"
    assert body["environment"]["type"] == "none"
    events = await sdk_http.get(
        f"/v1/agents/sessions/{body['id']}/events",
        headers={"Authorization": "Bearer sdk"},
    )
    assert events.status_code == 200
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["hello"]
