import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from tests.support.http import auth, session_with_turn, tenant_of
from tests.support.rows import row_json

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log, list_turn_logs
from apipi.worker.fake_harness import FAKE_USAGE, FakeHarness


async def test_completed_turn_writes_log_without_message_text(
    client: AsyncClient, store: Store
) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = tenant_of(token)
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
    assert row.run_mode == "none"
    assert row.artifact_bytes == 0
    blob = row_json(row)
    assert "hello" not in blob
    assert "secret-prompt" not in blob


async def test_turn_log_reads_are_tenant_scoped(
    client: AsyncClient, store: Store
) -> None:
    token_a = "a"
    token_b = "b"
    session_id = await session_with_turn(client, token_a)
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token_a)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    async with store.session() as db:
        assert await get_turn_log(db, tenant_of(token_a), turn_id) is not None
        assert await get_turn_log(db, tenant_of(token_b), turn_id) is None
        assert (
            await list_turn_logs(db, tenant_of(token_b), uuid.UUID(session_id)) is None
        )


@pytest.fixture
def mcp_log_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tavily_search"}]
    return harness


@pytest.fixture
async def mcp_log_client(
    settings: Settings,
    store: Store,
    mcp_log_harness: FakeHarness,
    worker_secret: str,
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=mcp_log_harness, token=worker_secret
    ) as (_app, client, _worker):
        yield client


async def test_completed_turn_log_counts_mcp_calls(
    mcp_log_client: AsyncClient, store: Store
) -> None:
    token = "mcp-log"
    session_id = await session_with_turn(mcp_log_client, token)
    turns = await mcp_log_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    async with store.session() as db:
        row = await get_turn_log(db, tenant_of(token), turn_id)
    assert row is not None
    assert row.mcp_names == ["mcp_tavily_search"]
    assert row.mcp_counts == {"mcp_tavily_search": 1}
    blob = row_json(row)
    assert "hello" not in blob
    assert "secret-reply" not in blob
