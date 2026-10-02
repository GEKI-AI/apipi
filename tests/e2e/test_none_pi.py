import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from tests.support.procs import split_http_client

from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.repo import get_session_turn

# Real `apipi serve --api-only` + `apipi worker` processes (run mode
# `none`, fake Pi). Hosted/microvm placement, pool reaping and workspace
# wipes are covered in tests/api/test_hosted_setup.py and
# tests/e2e/test_split_worker.py.
pytestmark = pytest.mark.e2e

_MAPPED_PI_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 8,
    "cache_read_tokens": 1,
    "cache_write_tokens": 2,
    "total_tokens": 16,
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def none_client(store: Store, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async with split_http_client(store, tmp_path) as client:
        yield client


async def test_none_fake_pi_persists_usage(
    none_client: AsyncClient, store: Store
) -> None:
    token = "e2e-usage"
    created_agent = await none_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await none_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello-none",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    completed = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.completed"
    ]
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-none"
    assert "assistantMessageEvent" not in done[0]["data"]
    assert len(completed) == 1
    usage = completed[0]["data"]["usage"]
    assert usage == _MAPPED_PI_USAGE
    assert "cost" not in usage
    assert "prompt" not in usage
    assert "secret-prompt" not in str(usage)
    turn_id = completed[0]["data"]["turn_id"]
    one = await none_client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert one.json()["usage"] == _MAPPED_PI_USAGE
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        row = await get_session_turn(
            db, tenant_id, uuid.UUID(session_id), uuid.UUID(turn_id)
        )
    assert row is not None
    assert row.usage == _MAPPED_PI_USAGE
