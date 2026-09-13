import json
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FAKE_USAGE, FakeHarness
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log, list_turn_logs
from apipi.tokens import hash_token


def _token(name: str = "t") -> str:
    return name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant_id(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _blob(row: object) -> str:
    table = getattr(row, "__table__", None)
    if table is None:
        return str(row)
    return json.dumps(
        {column.key: getattr(row, column.key) for column in table.columns},
        default=str,
    )


async def _session_with_turn(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_completed_turn_writes_log_without_message_text(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = _tenant_id(token)
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, turn_id)
        listed = await list_turn_logs(db, tenant_id, uuid.UUID(session_id))
    assert row is not None
    assert listed is not None
    assert len(listed) == 1
    assert row.status == "completed"
    assert row.session_id == uuid.UUID(session_id)
    assert row.turn_id == turn_id
    assert row.model == "test"
    assert row.agent_id is not None
    assert row.latency_ms >= 0
    assert row.prompt_tokens == FAKE_USAGE["prompt_tokens"]
    assert row.completion_tokens == FAKE_USAGE["completion_tokens"]
    assert row.cache_read_tokens == FAKE_USAGE["cache_read_tokens"]
    assert row.cache_write_tokens == FAKE_USAGE["cache_write_tokens"]
    assert row.total_tokens == FAKE_USAGE["total_tokens"]
    assert row.error_code is None
    assert row.request_id is not None
    assert row.request_id.isascii()
    assert len(row.request_id) <= 512
    assert row.tool_names == []
    assert row.tool_counts == {}
    assert row.mcp_names == []
    assert row.mcp_counts == {}
    assert row.environment_type == "none"
    assert row.run_mode == "host"
    assert row.artifact_bytes == 0
    blob = _blob(row)
    assert "hello" not in blob
    assert "secret-prompt" not in blob


async def test_turn_log_reads_are_tenant_scoped(
    client: AsyncClient, store: Store
) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id = await _session_with_turn(client, token_a)
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token_a)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    async with store.session() as db:
        assert await get_turn_log(db, _tenant_id(token_a), turn_id) is not None
        assert await get_turn_log(db, _tenant_id(token_b), turn_id) is None
        assert (
            await list_turn_logs(db, _tenant_id(token_b), uuid.UUID(session_id)) is None
        )


@pytest.fixture
def mcp_log_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tavily_search"}]
    return harness


@pytest.fixture
async def mcp_log_client(
    settings: Settings, store: Store, mcp_log_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=mcp_log_harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_completed_turn_log_counts_mcp_calls(
    mcp_log_client: AsyncClient, store: Store
) -> None:
    token = "mcp-log"
    session_id = await _session_with_turn(mcp_log_client, token)
    turns = await mcp_log_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    async with store.session() as db:
        row = await get_turn_log(db, _tenant_id(token), turn_id)
    assert row is not None
    assert row.mcp_names == ["mcp_tavily_search"]
    assert row.mcp_counts == {"mcp_tavily_search": 1}
    blob = _blob(row)
    assert "hello" not in blob
    assert "secret-reply" not in blob
