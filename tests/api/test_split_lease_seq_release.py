"""Split-mode regressions for #483: lease renewal, sequence, release order."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from tests.support.split_worker import (
    serve_split,
    split_client_for,
    wait_for_idle,
)

from apipi.config import Settings
from apipi.gateway.tokens import hash_token
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import WorkerIngest
from apipi.store.repo import get_session
from apipi.worker.fake_harness import FakeHarness

TOKEN = "lease-seq-release"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _tenant() -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(TOKEN))


async def _new_session(client: AsyncClient, **extra: Any) -> uuid.UUID:
    agent = await client.post(
        "/v1/agents", headers=_auth(), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            **extra,
        },
    )
    assert created.status_code == 200, created.text
    return uuid.UUID(created.json()["id"])


async def _message(client: AsyncClient, session_id: uuid.UUID, text: str) -> Any:
    return await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(),
        json={"type": "agent.session.input.message", "content": text},
    )


async def _until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def _lease_of(store: Store, session_id: uuid.UUID) -> Any:
    async with store.session() as db:
        row = await get_session(db, _tenant(), session_id)
        assert row is not None
        return row.lease_id


async def _ledger(store: Store, session_id: uuid.UUID) -> dict[int, str]:
    async with store.session() as db:
        rows = await db.scalars(
            select(WorkerIngest).where(WorkerIngest.session_id == session_id)
        )
        return {row.worker_seq: row.envelope_type for row in rows}


async def _lease_command(app: Any, store: Any, session_id: uuid.UUID) -> dict[str, Any]:
    command = await app.state.workers.acquire(
        store, _tenant(), session_id, op="turn.cancel"
    )
    assert command is not None
    assert await app.state.workers.wait_ack(
        uuid.UUID(command["lease_id"]), command["id"]
    )
    return command


def _error_event(text: str) -> dict[str, Any]:
    return {
        "type": "agent.session.error",
        "data": {"message": text, "code": "worker_test"},
    }


async def test_busy_turn_keeps_its_lease(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    short = settings.model_copy(update={"worker_lease_ttl": timedelta(seconds=1)})
    async with split_client_for(short, store, token=worker_secret) as (
        app,
        client,
        worker,
    ):
        session_id = await _new_session(client)
        command = await _lease_command(app, store, session_id)
        lease_id = uuid.UUID(command["lease_id"])
        ticks = 0
        loop = asyncio.get_running_loop()
        end = loop.time() + 2.5
        while loop.time() < end:
            worker.outbox.append(session_id, "event", _error_event(f"tick {ticks}"))
            ticks += 1
            expired = await app.state.workers.expire(store, app.state.event_hub)
            assert expired == []
            await asyncio.sleep(0.1)
        assert await _lease_of(store, session_id) == lease_id

        async def stored() -> bool:
            async with store.session() as db:
                events = await list_events(db, _tenant(), session_id)
            errors = [e for e in events if e.type == "agent.session.error"]
            return len(errors) == ticks

        await _until(stored)


async def test_second_worker_continues_the_session_sequence(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        first,
    ):
        session_id = await _new_session(client)
        done = await _message(client, session_id, "one")
        assert done.status_code == 200
        first_high_water = first.outbox.high_water(session_id)
        assert first_high_water > 5

        async def acked() -> bool:
            return first.outbox.acked_seq(session_id) == first_high_water

        await _until(acked)
        await first.execution.note_stopped(session_id)

        async def released() -> bool:
            return await _lease_of(store, session_id) is None

        await _until(released)
        await first.aclose()

        async def gone() -> bool:
            return app.state.workers.live() == 0

        await _until(gone)
        second_token = (await create_token(store, name="worker-b")).secret
        async with serve_split(app, settings, FakeHarness(), second_token) as second:
            assert second.outbox.high_water(session_id) == 0
            reply = await asyncio.wait_for(
                _message(client, session_id, "two"), timeout=15
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["status"] == "idle"
            assert second.outbox.high_water(session_id) > first_high_water
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth()
        )
        completed = [
            e
            for e in events.json()["data"]
            if e["type"] == "agent.session.turn.completed"
        ]
        assert len(completed) == 2
        ledger = await _ledger(store, session_id)
        assert max(ledger) > first_high_water


async def test_release_does_not_overtake_buffered_envelopes(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        worker,
    ):
        session_id = await _new_session(client)
        await _lease_command(app, store, session_id)
        stop = worker.outbox.append(
            session_id, "lifecycle.stop", {"reason": "idle", "live_ms": 5}
        )
        marker = worker.outbox.append(session_id, "event", _error_event("last words"))
        await worker.execution.note_stopped(session_id)

        async def released() -> bool:
            return await _lease_of(store, session_id) is None

        await _until(released)
        await asyncio.sleep(0.2)
        ledger = await _ledger(store, session_id)
        assert ledger[stop["seq"]] == "lifecycle.stop"
        assert ledger[marker["seq"]] == "event"
        async with store.session() as db:
            events = await list_events(db, _tenant(), session_id)
        assert any(
            e.type == "agent.session.error" and e.data["message"] == "last words"
            for e in events
        )


async def test_session_stop_receipt_is_ingested_before_the_release(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        worker,
    ):
        session_id = await _new_session(client)
        done = await _message(client, session_id, "hi")
        assert done.status_code == 200
        await wait_for_idle(client, TOKEN, str(session_id))
        await app.state.execution.teardown(session_id)
        assert await _lease_of(store, session_id) is None
        ledger = await _ledger(store, session_id)
        assert "session.stopped" in ledger.values()
        assert worker.outbox.acked_seq(session_id) == worker.outbox.high_water(
            session_id
        )
