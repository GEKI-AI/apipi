import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.http import auth, tenant_of

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.turn_logs import get_turn_log
from apipi.worker.fake_harness import FakeHarness


def _app_of(client: AsyncClient) -> FastAPI:
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    return cast(FastAPI, transport.app)


@pytest.fixture
def cancel_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.hold = True
    return harness


@pytest.fixture
async def cancel_client(
    settings: Settings,
    store: Store,
    cancel_harness: FakeHarness,
    worker_secret: str,
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=cancel_harness, token=worker_secret
    ) as (_app, client, _worker):
        yield client


async def _create_idle_session(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def test_cancel_in_progress_turn(
    cancel_client: AsyncClient, store: Store
) -> None:
    token = "c"
    session_id = await _create_idle_session(cancel_client, token)
    sid = uuid.UUID(session_id)
    hub = _app_of(cancel_client).state.event_hub
    queue = hub.subscribe(sid)
    try:
        task = asyncio.create_task(
            cancel_client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=auth(token),
                json={"type": "agent.session.input.message", "content": "go"},
            )
        )
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=2)
            if event["type"] == "agent.session.in_progress":
                break
        cancelled = await cancel_client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=auth(token),
            json={"type": "agent.session.input.cancel"},
        )
        assert cancelled.status_code == 200
        posted = await task
        assert posted.status_code == 200
        assert posted.json()["status"] == "idle"
    finally:
        hub.unsubscribe(sid, queue)

    got = await cancel_client.get(
        f"/v1/agents/sessions/{session_id}", headers=auth(token)
    )
    assert got.json()["status"] == "idle"
    turns = await cancel_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    assert turns.json()["data"][0]["status"] == "cancelled"
    events = await cancel_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    index = types.index("agent.session.turn.cancelled")
    assert types[index + 1] == "agent.session.idle"
    assert types[-1] == "agent.session.idle"
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = tenant_of(token)
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, turn_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.prompt_tokens == 0
    assert row.total_tokens == 0
    assert "go" not in str(row.tool_names) + str(row.mcp_names) + str(row.model)


async def test_cancel_idle_is_invalid(cancel_client: AsyncClient) -> None:
    session_id = await _create_idle_session(cancel_client, "c")
    response = await cancel_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth("c"),
        json={"type": "agent.session.input.cancel"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_message_cancels_live_in_progress(
    cancel_client: AsyncClient,
    cancel_harness: FakeHarness,
) -> None:
    token = "c"
    session_id = await _create_idle_session(cancel_client, token)
    sid = uuid.UUID(session_id)
    hub = _app_of(cancel_client).state.event_hub
    queue = hub.subscribe(sid)
    try:
        task = asyncio.create_task(
            cancel_client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=auth(token),
                json={"type": "agent.session.input.message", "content": "go"},
            )
        )
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=2)
            if event["type"] == "agent.session.in_progress":
                break
        cancel_harness.hold = False
        second = await cancel_client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=auth(token),
            json={"type": "agent.session.input.message", "content": "next"},
        )
        first = await task
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "idle"
    finally:
        hub.unsubscribe(sid, queue)
    turns = await cancel_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    statuses = [row["status"] for row in turns.json()["data"]]
    assert "cancelled" in statuses
    assert "completed" in statuses
