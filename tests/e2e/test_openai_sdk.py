import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store

pytestmark = pytest.mark.slow

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "openai_sdk.py"


def _load_example() -> Any:
    spec = importlib.util.spec_from_file_location("openai_sdk_example", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    example = _load_example()
    result = await example.create_agent_and_read_turn(sdk)
    agent = result["agent"]
    assert agent["name"] == "demo"
    assert agent["model"] == "gpt-4.1"
    session = result["session"]
    assert session["status"] == "idle"
    assert session["environment"]["type"] == "none"
    turn = result["turn"]
    assert turn["status"] == "completed"
    assert turn["session_id"] == session["id"]
    events = await sdk_http.get(
        f"/v1/agents/sessions/{session['id']}/events",
        headers={"Authorization": "Bearer sdk"},
    )
    assert events.status_code == 200
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["Hello"]
