from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from apipi.config import Settings
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
    settings: Settings,
    store: Store,
    mcp_harness: FakeHarness,
    worker_secret: str,
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=mcp_harness, token=worker_secret
    ) as (_app, client, _worker):
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
    mcp_url, _seen = mcp_server
    token = "mcp"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer static-secret"},
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
    assert mcp_harness.mcp_http[0].headers == {"Authorization": "Bearer static-secret"}
    session_id = created.json()["id"]
    events = await mcp_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.failed" not in types
    assert types[-1] == "agent.session.idle"
    dumped = str(events.json())
    assert "Authorization" not in dumped
    assert "static-secret" not in dumped


async def test_mcp_http_on_none_session(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, _seen = mcp_server
    token = "mcp-none"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer static-secret"},
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
    assert created.json()["environment"]["type"] == "none"
    assert created.json()["status"] == "idle"
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].server_label == "mock"
    assert mcp_harness.mcp_http[0].headers == {"Authorization": "Bearer static-secret"}


async def test_mcp_http_dead_server_no_longer_fails_create(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_fail_url: str,
) -> None:
    token = "mcp"
    agent_id = await _agent_with_mcp(mcp_client, token, mcp_fail_url)
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


async def test_mcp_http_vault_only_server_starts(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, _seen = mcp_server
    token = "mcp-vault"
    vault = await mcp_client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "v"}
    )
    assert vault.status_code == 200
    cred = await mcp_client.post(
        f"/v1/agents/vaults/{vault.json()['id']}/credentials",
        headers=_auth(token),
        json={
            "name": "c",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": mcp_url,
                "token": "vault-secret",
            },
        },
    )
    assert cred.status_code == 200
    agent_id = await _agent_with_mcp(mcp_client, token, mcp_url)
    created = await mcp_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "vault_ids": [vault.json()["id"]],
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "idle"
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].headers == {"Authorization": "Bearer vault-secret"}


async def test_mcp_http_followup_turn_uses_live_agent(
    mcp_client: AsyncClient,
    mcp_harness: FakeHarness,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, _seen = mcp_server
    token = "mcp-live"
    agent_id = await _agent_with_mcp(
        mcp_client,
        token,
        mcp_url,
        headers={"Authorization": "Bearer first"},
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
    session_id = created.json()["id"]
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].headers == {"Authorization": "Bearer first"}
    updated = await mcp_client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "mock",
                    "server_url": mcp_url,
                    "headers": {"Authorization": "Bearer second"},
                }
            ]
        },
    )
    assert updated.status_code == 200
    again = await mcp_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert again.status_code == 200
    assert mcp_harness.mcp_http is not None
    assert mcp_harness.mcp_http[0].headers == {"Authorization": "Bearer second"}


async def test_mcp_http_env_reference_fails(
    mcp_client: AsyncClient, mcp_url: str
) -> None:
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
