import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.http import auth, tenant_of
from tests.support.ingest import frame
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import record_search_usage, set_session_lease


def _api_settings(settings: Settings) -> Settings:
    return api_settings_for(
        Settings(
            database_url=settings.database_url,
            run_mode="none",
            sessions_dir=settings.sessions_dir,
        ),
        batch_window_zero=False,
    )


async def _session(app: FastAPI, token: str) -> uuid.UUID:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
        )
        assert agent.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        assert created.status_code == 200
        return uuid.UUID(created.json()["id"])


def _start(session_id: uuid.UUID, turn_id: uuid.UUID) -> list[dict[str, Any]]:
    flow = [
        ("session.status", {"status": "in_progress"}),
        ("turn.status", {"turn_id": str(turn_id), "status": "started"}),
    ]
    return [frame(session_id, i + 1, t, p) for i, (t, p) in enumerate(flow)]


def _search_item(
    session_id: uuid.UUID, turn_id: uuid.UUID, item_id: uuid.UUID, start: int
) -> list[dict[str, Any]]:
    flow = [
        (
            "item.added",
            {
                "item_id": str(item_id),
                "item_type": "web_search_call",
                "turn_id": str(turn_id),
                "data": {
                    "status": "in_progress",
                    "action": {"type": "search", "query": "apipi"},
                },
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.item.added",
                "data": {
                    "item_id": str(item_id),
                    "item_type": "web_search_call",
                    "turn_id": str(turn_id),
                },
                "turn_id": str(turn_id),
            },
        ),
        (
            "item.done",
            {
                "item_id": str(item_id),
                "turn_id": str(turn_id),
                "data": {"status": "completed"},
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.item.done",
                "data": {"item_id": str(item_id), "turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
    ]
    return [frame(session_id, start + i, t, p) for i, (t, p) in enumerate(flow)]


def _finish(
    session_id: uuid.UUID, turn_id: uuid.UUID, start: int
) -> list[dict[str, Any]]:
    flow = [
        ("turn.status", {"turn_id": str(turn_id), "status": "completed"}),
        (
            "usage",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        ),
        (
            "event",
            {
                "type": "agent.session.turn.completed",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        ("session.status", {"status": "idle"}),
    ]
    return [frame(session_id, start + i, t, p) for i, (t, p) in enumerate(flow)]


async def _drain(worker: FakeWorker, last_seq: int) -> None:
    while True:
        message = await worker.receive_json(timeout=10)
        if (
            message.get("type") == "ack"
            and int(message.get("last_seq") or 0) >= last_seq
        ):
            return


async def _record(
    store: Store,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    provider: str = "tavily",
    calls: int = 1,
    units: int = 1,
) -> None:
    async with store.session() as db:
        await record_search_usage(
            db,
            tenant_id,
            session_id,
            turn_id,
            provider=provider,
            key_source="operator",
            calls=calls,
            units=units,
        )


async def _setup(
    settings: Settings, store: Store, worker_secret: str
) -> tuple[FastAPI, FakeWorker, uuid.UUID, uuid.UUID]:
    app = create_app(_api_settings(settings), store=store)
    session_id = await _session(app, "t")
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    async with store.session() as db:
        await set_session_lease(
            db,
            tenant_of("t"),
            session_id,
            worker_id=uuid.UUID(str(hello["worker_id"])),
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    return app, worker, tenant_of("t"), session_id


async def _usage(client: AsyncClient, token: str, **params: str) -> dict[str, Any]:
    response = await client.get("/v1/apipi/usage", headers=auth(token), params=params)
    assert response.status_code == 200
    return response.json()


async def test_web_search_item_is_stored_and_served(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app, worker, _tenant_id, session_id = await _setup(settings, store, worker_secret)
    turn_id = uuid.uuid4()
    item_id = uuid.uuid4()
    envelopes = (
        _start(session_id, turn_id)
        + _search_item(session_id, turn_id, item_id, 3)
        + _finish(session_id, turn_id, 7)
    )
    for envelope in envelopes:
        await worker.send_json(envelope)
    await _drain(worker, 10)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        items = await client.get(
            f"/v1/agents/sessions/{session_id}/items", headers=auth("t")
        )
        assert items.status_code == 200
        data = items.json()["data"]
        assert [item["type"] for item in data] == ["web_search_call"]
        assert data[0]["id"] == str(item_id)
        assert data[0]["turn_id"] == str(turn_id)
        assert data[0]["data"] == {
            "status": "completed",
            "action": {"type": "search", "query": "apipi"},
        }
        exported = await client.get(
            f"/v1/apipi/sessions/{session_id}/export", headers=auth("t")
        )
        assert exported.status_code == 200
        assert [item["type"] for item in exported.json()["items"]] == [
            "web_search_call"
        ]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=auth("t")
        )
        added = [
            event["data"]
            for event in events.json()["data"]
            if event["type"] == "agent.session.turn.item.added"
        ]
        assert [entry["item_type"] for entry in added] == ["web_search_call"]
    await worker.close()


@pytest.mark.parametrize("search_first", [True, False])
async def test_usage_endpoint_counts_search_in_either_order(
    settings: Settings, store: Store, worker_secret: str, search_first: bool
) -> None:
    app, worker, tenant_id, session_id = await _setup(settings, store, worker_secret)
    turn_id = uuid.uuid4()
    for envelope in _start(session_id, turn_id):
        await worker.send_json(envelope)
    await _drain(worker, 2)
    day = datetime.now(UTC).date().isoformat()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        if search_first:
            await _record(store, tenant_id, session_id, turn_id, units=2)
            await _record(store, tenant_id, session_id, turn_id, units=2)
            early = await _usage(client, "t", turn_id=str(turn_id))
            assert early["search_calls"] == 2
            assert early["search_units"] == 4
            assert early["turns"] == 0
        for envelope in _finish(session_id, turn_id, 3):
            await worker.send_json(envelope)
        await _drain(worker, 6)
        if not search_first:
            before = await _usage(client, "t", day=day)
            assert before["search_calls"] == 0
            assert before["search_units"] == 0
            await _record(store, tenant_id, session_id, turn_id, provider="staan")
            await _record(store, tenant_id, session_id, turn_id, units=3)
        for params in (
            {"session_id": str(session_id)},
            {"turn_id": str(turn_id)},
            {"day": day},
        ):
            body = await _usage(client, "t", **params)
            assert body["search_calls"] == 2
            assert body["search_units"] == 4
            assert body["turns"] == 1
            assert body["prompt_tokens"] == 11
    await worker.close()
