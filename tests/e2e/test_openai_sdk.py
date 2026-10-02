from typing import Any

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.slow


async def test_openai_sdk_subset(client: AsyncClient) -> None:
    # `client` is the split-mode fixture: API app plus an in-process worker.
    sdk_http = client
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
    raw = getattr(agents.sessions, "with_raw_response", None)
    if raw is None:
        pytest.skip("openai SDK has no with_raw_response")
    created = await raw.create(
        agent={
            "model": "gpt-4.1",
            "instructions": "Write clean code, run it, and report the actual output.",
        },
        environment={"type": "openai_hosted"},
        input="Hello",
    )
    assert created.status_code == 200
    session = created.http_response.json()
    assert isinstance(session, dict)
    assert session["status"] == "idle"
    assert session["environment"]["type"] == "openai_hosted"
    session_id = session["id"]
    events = await sdk_http.get(
        f"/v1/agents/sessions/{session_id}/events",
        headers={"Authorization": "Bearer sdk"},
    )
    assert events.status_code == 200
    payload: Any = events.json()
    types = [event["type"] for event in payload["data"]]
    assert "agent.session.created" in types
    assert "agent.session.turn.completed" in types
    texts = [
        event["data"]["text"]
        for event in payload["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["Hello"]
