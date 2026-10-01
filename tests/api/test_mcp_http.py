from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store

pytest_plugins = ["tests.support.mcp_http_server"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(Path(tmp_path) / "sessions"),
        mcp_allow_hosts="127.0.0.1",
    )


@pytest.fixture
def mcp_harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
async def mcp_client(
    settings: Settings, store: Store, mcp_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=mcp_harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _agent_with_mcp(
    client: AsyncClient, token: str, url: str, headers: dict[str, str] | None = None
) -> str:
    tool: dict[str, object] = {
        "type": "mcp",
        "server_label": "mock",
        "server_url": url,
    }
    if headers is not None:
        tool["headers"] = headers
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "tools": [tool]},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_mcp_http_starts_with_session(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, seen = mcp_server
    token = "mcp"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer from-env"},
    )
    created = await mcp_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "idle"
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].server_label == "mock"
    assert seen.get("Authorization") == "Bearer from-env"
    session_id = created.json()["id"]
    events = await mcp_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.failed" not in types
    assert types[-1] == "agent.session.idle"
    dumped = str(events.json())
    assert "Authorization" not in dumped
    assert "from-env" not in dumped


async def test_mcp_http_on_chat_session(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, seen = mcp_server
    token = "mcp-chat"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer from-env"},
    )
    created = await mcp_client.post(
        "/v1/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello"},
    )
    assert created.status_code == 200
    assert "environment" not in created.json()
    assert created.json()["status"] == "idle"
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].server_label == "mock"
    assert seen.get("Authorization") == "Bearer from-env"


async def test_mcp_http_failure_is_session_failed(
    mcp_client: AsyncClient, mcp_fail_url: str
) -> None:
    token = "mcp"
    agent_id = await _agent_with_mcp(mcp_client, token, mcp_fail_url)
    created = await mcp_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "failed"
    session_id = created.json()["id"]
    events = await mcp_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.error" in types
    assert types[-1] == "agent.session.failed"
    other = await mcp_client.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth("other")
    )
    assert other.status_code == 404


async def test_mcp_http_missing_env_fails(
    mcp_client: AsyncClient, mcp_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_TOKEN", raising=False)
    token = "mcp"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer ${MCP_TOKEN}"},
    )
    created = await mcp_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "failed"


async def test_mcp_http_env_header_fails_session(
    mcp_client: AsyncClient, mcp_server: tuple[str, dict[str, str]]
) -> None:
    mcp_url, _seen = mcp_server
    token = "mcp-env"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer ${MCP_TOKEN}"},
    )
    created = await mcp_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "failed"
    session_id = created.json()["id"]
    events = await mcp_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert "vault" in str(events.json())
