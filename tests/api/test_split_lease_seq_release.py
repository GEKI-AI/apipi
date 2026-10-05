"""Split-mode regressions for #483: lease renewal, sequence, release order."""

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from tests.support.http import auth, post_message, tenant_of
from tests.support.split_worker import serve_split, split_client_for, wait_for_idle
from tests.support.waits import until

from apipi.config import Settings
from apipi.protocol import FEATURE_SESSION_STOPPED
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import WorkerIngest
from apipi.store.repo import get_session
from apipi.worker.fake_harness import FakeHarness

TOKEN = "lease-seq-release"


async def _new_session(client: AsyncClient, **extra: Any) -> uuid.UUID:
    agent = await client.post(
        "/v1/agents", headers=auth(TOKEN), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(TOKEN),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            **extra,
        },
    )
    assert created.status_code == 200, created.text
    return uuid.UUID(created.json()["id"])


async def _lease_of(store: Store, session_id: uuid.UUID) -> Any:
    async with store.session() as db:
        row = await get_session(db, tenant_of(TOKEN), session_id)
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
        store, tenant_of(TOKEN), session_id, op="turn.cancel"
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
    short = settings.model_copy(
        update={"worker_lease_ttl": timedelta(milliseconds=500)}
    )
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
        end = loop.time() + 1.5
        while loop.time() < end:
            worker.outbox.append(session_id, "event", _error_event(f"tick {ticks}"))
            ticks += 1
            expired = await app.state.workers.expire(store, app.state.event_hub)
            assert expired == []
            await asyncio.sleep(0.05)
        assert await _lease_of(store, session_id) == lease_id

        async def stored() -> bool:
            async with store.session() as db:
                events = await list_events(db, tenant_of(TOKEN), session_id)
            errors = [e for e in events if e.type == "agent.session.error"]
            return len(errors) == ticks

        await until(stored, timeout=10.0)


async def test_second_worker_continues_the_session_sequence(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        first,
    ):
        session_id = await _new_session(client)
        done = await post_message(client, TOKEN, session_id, "one")
        assert done.status_code == 200
        first_high_water = first.outbox.high_water(session_id)
        assert first_high_water > 5

        async def acked() -> bool:
            return first.outbox.acked_seq(session_id) == first_high_water

        await until(acked, timeout=10.0)
        await first.execution.note_stopped(session_id)

        async def released() -> bool:
            return await _lease_of(store, session_id) is None

        await until(released, timeout=10.0)
        await first.aclose()

        async def gone() -> bool:
            return app.state.workers.live() == 0

        await until(gone, timeout=10.0)
        second_token = (await create_token(store, name="worker-b")).secret
        async with serve_split(app, settings, FakeHarness(), second_token) as second:
            assert second.outbox.high_water(session_id) == 0
            reply = await asyncio.wait_for(
                post_message(client, TOKEN, session_id, "two"), timeout=15
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["status"] == "idle"
            assert second.outbox.high_water(session_id) > first_high_water
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=auth(TOKEN)
        )
        completed = [
            e
            for e in events.json()["data"]
            if e["type"] == "agent.session.turn.completed"
        ]
        assert len(completed) == 2
        ledger = await _ledger(store, session_id)
        assert max(ledger) > first_high_water


async def test_session_stop_is_acked_on_receipt_and_completed_by_the_envelope(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    sent: list[str] = []
    async with split_client_for(settings, store, token=worker_secret, sent=sent) as (
        app,
        client,
        worker,
    ):
        session_id = await _new_session(client)
        assert (await post_message(client, TOKEN, session_id, "hi")).status_code == 200
        await wait_for_idle(client, TOKEN, str(session_id))
        assert FEATURE_SESSION_STOPPED in app.state.workers.features_of(
            next(iter(app.state.workers._conns))
        )
        await app.state.execution.teardown(session_id)
        assert await _lease_of(store, session_id) is None
        assert "session.stopped" in (await _ledger(store, session_id)).values()
        frames = [json.loads(text) for text in sent]
        acks = [i for i, f in enumerate(frames) if f.get("type") == "lease.ack"]
        stopped = [
            i for i, f in enumerate(frames) if f.get("type") == "session.stopped"
        ]
        assert acks and stopped
        assert acks[-1] < stopped[0]
        assert worker.outbox.acked_seq(session_id) == worker.outbox.high_water(
            session_id
        )
