"""Two API replicas, one store, one worker: commands cross replicas (#490).

The worker holds its socket on replica A. Every request goes to replica
B, which has no socket, so each command must be forwarded.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from tests.support.notify_bus import NotifyNetwork
from tests.support.split_worker import (
    SplitWorker,
    api_settings_for,
    spawn_split_worker,
    wait_for_idle,
    worker_settings_for,
)

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.models import SessionRow, WorkerForward, WorkerRow, utc_now
from apipi.store.repo import get_session
from apipi.worker.fake_harness import FakeHarness

TOKEN = "replicas"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _tenant() -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(TOKEN))


@dataclass
class Replicas:
    app_a: FastAPI
    app_b: FastAPI
    client_a: AsyncClient
    client_b: AsyncClient
    worker: SplitWorker
    network: NotifyNetwork
    store: Store
    calls: list[tuple[str, str]]

    @property
    def hub_a(self) -> Any:
        return self.app_a.state.workers

    @property
    def hub_b(self) -> Any:
        return self.app_b.state.workers


async def _until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _stop_loops(app: FastAPI, *names: str) -> None:
    for task in list(app.state.gateway._tasks):
        if task.get_name() in (names or ("lease_reaper", "command_retransmit")):
            task.cancel()


def _spy(app: FastAPI, calls: list[tuple[str, str]]) -> None:
    forwarder = app.state.workers.forwarder
    assert forwarder is not None
    original = forwarder.call

    async def call(target: str, **kwargs: Any) -> Any:
        calls.append((kwargs["action"], kwargs["op"]))
        return await original(target, **kwargs)

    forwarder.call = call


@pytest.fixture
def replica_harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
async def replicas(
    settings: Settings,
    store: Store,
    worker_secret: str,
    replica_harness: FakeHarness,
) -> AsyncIterator[Replicas]:
    network = NotifyNetwork()
    api_settings = api_settings_for(settings).model_copy(
        update={"event_bus_fallback_poll": timedelta(milliseconds=100)}
    )
    app_a = create_app(
        api_settings.model_copy(update={"instance_id": "node-a"}),
        store=store,
        event_hub=network.bus(),
    )
    app_b = create_app(
        api_settings.model_copy(update={"instance_id": "node-b"}),
        store=store,
        event_hub=network.bus(),
    )
    for app in (app_a, app_b):
        await app.state.gateway.startup()
        _stop_loops(app)
    calls: list[tuple[str, str]] = []
    _spy(app_b, calls)
    worker = await spawn_split_worker(
        app_a, worker_settings_for(settings), replica_harness, worker_secret
    )
    async with (
        AsyncClient(
            transport=ASGITransport(app=app_a), base_url="http://a"
        ) as client_a,
        AsyncClient(
            transport=ASGITransport(app=app_b), base_url="http://b"
        ) as client_b,
    ):
        try:
            yield Replicas(
                app_a, app_b, client_a, client_b, worker, network, store, calls
            )
        finally:
            await worker.aclose()
            for app in (app_a, app_b):
                await app.state.gateway.shutdown()


async def _new_session(
    client: AsyncClient, environment: dict[str, Any] | None = None, **extra: Any
) -> uuid.UUID:
    agent = await client.post(
        "/v1/agents",
        headers=_auth(),
        json={"name": "bot", "model": "test", **extra.pop("agent", {})},
    )
    assert agent.status_code == 200, agent.text
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(),
        json={
            "agent_id": agent.json()["id"],
            "environment": environment or {"type": "none"},
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


async def _in_progress(replicas: Replicas, session_id: uuid.UUID) -> None:
    async def running() -> bool:
        got = await replicas.client_b.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth()
        )
        return got.json()["status"] == "in_progress"

    await _until(running)


async def _lease(store: Store, session_id: uuid.UUID) -> tuple[Any, Any]:
    async with store.session() as db:
        row = await get_session(db, _tenant(), session_id)
        assert row is not None
        return row.worker_id, row.lease_id


async def _forward_rows(store: Store) -> list[WorkerForward]:
    async with store.session() as db:
        return list(await db.scalars(select(WorkerForward)))


async def _no_forward_rows(store: Store) -> None:
    async def empty() -> bool:
        return await _forward_rows(store) == []

    await _until(empty)


async def test_turn_start_is_forwarded_without_the_body_on_the_bus(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(replicas.client_b)
    text = "x" * 100_000
    posted = await _message(replicas.client_b, session_id, text)
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "idle"
    assert replicas.calls == [("acquire", "turn.start")]
    worker_id, lease_id = await _lease(replicas.store, session_id)
    assert worker_id is not None and lease_id in replicas.hub_a._conns[worker_id].leases
    assert replicas.hub_b._conns == {}
    sizes = [size for _target, _message_body, size in replicas.network.sent]
    assert sizes and max(sizes) < 400
    await _no_forward_rows(replicas.store)
    items = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth()
    )
    assert text in str(items.json())


async def test_follow_up_turn_is_forwarded_as_a_command(replicas: Replicas) -> None:
    session_id = await _new_session(replicas.client_a)
    first = await _message(replicas.client_a, session_id, "one")
    assert first.status_code == 200, first.text
    second = await _message(replicas.client_b, session_id, "two")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "idle"
    assert replicas.calls == [("command", "turn.start")]


async def test_turn_continue_is_forwarded(
    replicas: Replicas, replica_harness: FakeHarness
) -> None:
    replica_harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    session_id = await _new_session(
        replicas.client_a,
        agent={
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        },
        input="use echo",
    )
    got = await replicas.client_a.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth()
    )
    assert got.json()["status"] == "requires_action"
    events = await replicas.client_a.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth()
    )
    turn_id = next(
        event["data"]["turn_id"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    )
    resumed = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "pong",
        },
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "idle"
    assert replicas.calls == [("command", "turn.continue")]


async def test_turn_cancel_is_forwarded(
    replicas: Replicas, replica_harness: FakeHarness
) -> None:
    replica_harness.hold = True
    session_id = await _new_session(replicas.client_b)
    task = asyncio.create_task(_message(replicas.client_b, session_id, "go"))
    await _in_progress(replicas, session_id)
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    posted = await asyncio.wait_for(task, timeout=10)
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    assert ("command", "turn.cancel") in replicas.calls
    turns = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth()
    )
    assert turns.json()["data"][0]["status"] == "cancelled"


async def test_session_stop_is_forwarded_and_waits(replicas: Replicas) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (await _message(replicas.client_a, session_id, "hi")).status_code == 200
    worker_id, lease_id = await _lease(replicas.store, session_id)
    deleted = await replicas.client_b.delete(
        f"/v1/agents/sessions/{session_id}", headers=_auth()
    )
    assert deleted.status_code == 200, deleted.text
    assert ("command", "session.stop") in replicas.calls
    assert lease_id not in replicas.hub_a._conns[worker_id].leases
    assert session_id not in replicas.worker.session_leases
    await _no_forward_rows(replicas.store)
    gone = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth()
    )
    assert gone.status_code == 404


async def test_sandbox_boot_is_forwarded(replicas: Replicas) -> None:
    session_id = await _new_session(
        replicas.client_b, environment={"type": "openai_hosted"}
    )
    await replicas.app_b.state.execution.boot_hosted(_tenant(), session_id)
    assert replicas.calls == [("acquire", "sandbox.boot")]
    worker_id, lease_id = await _lease(replicas.store, session_id)
    assert worker_id is not None
    assert lease_id in replicas.hub_a._conns[worker_id].leases

    async def acked() -> bool:
        return len(replicas.hub_a.commands) == 0

    await _until(acked)


async def test_lease_revoke_from_the_reaper_reaches_the_socket(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (await _message(replicas.client_a, session_id, "hi")).status_code == 200
    worker_id, lease_id = await _lease(replicas.store, session_id)
    assert lease_id in replicas.hub_a._conns[worker_id].leases
    async with replicas.store.session() as db:
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id)
            .values(lease_until=utc_now() - timedelta(seconds=5))
        )
    expired = await replicas.hub_b.expire(
        replicas.store, replicas.app_b.state.event_hub
    )
    assert expired == [session_id]
    assert ("revoke", "lease.revoke") in replicas.calls

    async def revoked() -> bool:
        return (
            lease_id not in replicas.hub_a._conns[worker_id].leases
            and session_id not in replicas.worker.session_leases
        )

    await _until(revoked)


async def test_cancel_that_cannot_reach_the_worker_is_an_error(
    replicas: Replicas, replica_harness: FakeHarness
) -> None:
    replica_harness.hold = True
    session_id = await _new_session(replicas.client_b)
    task = asyncio.create_task(_message(replicas.client_b, session_id, "go"))
    await _in_progress(replicas, session_id)
    replicas.network.dropped.add(replicas.hub_a.instance_id)
    _stop_loops(replicas.app_a, "worker_forwards")
    replicas.hub_b.forwarder.sent_timeout = 0.3
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 504, cancelled.text
    assert cancelled.json()["error"]["code"] == "forward_timeout"
    await _no_forward_rows(replicas.store)
    replicas.network.dropped.clear()
    replica_harness.hold = False
    task.cancel()


async def test_stale_replica_fails_fast_with_a_clear_code(
    replicas: Replicas, replica_harness: FakeHarness
) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (await _message(replicas.client_a, session_id, "hi")).status_code == 200
    worker_id, _lease_id = await _lease(replicas.store, session_id)
    async with replicas.store.session() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker_id)
            .values(last_seen=utc_now() - timedelta(minutes=5))
        )
    started = asyncio.get_running_loop().time()
    follow = await _message(replicas.client_b, session_id, "again")
    assert follow.status_code == 503, follow.text
    assert follow.json()["error"]["code"] == "worker_unreachable"
    assert asyncio.get_running_loop().time() - started < 2
    assert replicas.network.sent == []


async def test_placement_sees_workers_on_other_replicas(
    replicas: Replicas, settings: Settings, store: Store
) -> None:
    second_token = (await create_token(store, name="second")).secret
    second = await spawn_split_worker(
        replicas.app_b, worker_settings_for(settings), FakeHarness(), second_token
    )
    try:
        first_id = await _new_session(replicas.client_a)
        second_id = await _new_session(replicas.client_a)
        assert (await _message(replicas.client_a, first_id, "one")).status_code == 200
        assert (await _message(replicas.client_a, second_id, "two")).status_code == 200
        workers = {
            (await _lease(replicas.store, first_id))[0],
            (await _lease(replicas.store, second_id))[0],
        }
        assert len(workers) == 2
    finally:
        await second.aclose()


async def test_capacity_counts_leases_held_through_other_replicas(
    replicas: Replicas, settings: Settings, store: Store
) -> None:
    async with store.session() as db:
        await db.execute(update(WorkerRow).values(capacity=1))
    replicas.hub_a._conns[next(iter(replicas.hub_a._conns))].capacity = 1
    first_id = await _new_session(replicas.client_b)
    assert (await _message(replicas.client_b, first_id, "one")).status_code == 200
    second_id = await _new_session(replicas.client_b)
    full = await _message(replicas.client_b, second_id, "two")
    assert full.status_code == 429, full.text
    assert full.json()["error"]["code"] == "capacity"
    await wait_for_idle(replicas.client_b, TOKEN, str(first_id))


async def test_a_forward_claimed_twice_is_sent_once(replicas: Replicas) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (await _message(replicas.client_a, session_id, "hi")).status_code == 200
    worker_id, _lease_id = await _lease(replicas.store, session_id)
    forwarder = replicas.hub_a.forwarder
    forward_id = uuid.uuid4()
    async with replicas.store.session() as db:
        db.add(
            WorkerForward(
                id=forward_id,
                action="command",
                op="turn.cancel",
                wait="none",
                tenant_id=_tenant(),
                session_id=session_id,
                worker_id=worker_id,
                target=replicas.hub_a.instance_id,
                origin=replicas.hub_b.instance_id,
                body={"payload": {"tenant_id": str(_tenant())}},
                status="pending",
            )
        )
    sent: list[str] = []
    original = replicas.hub_a.command

    async def command(*args: Any, **kwargs: Any) -> Any:
        sent.append(str(kwargs.get("command_id")))
        return await original(*args, **kwargs)

    replicas.hub_a.command = command
    await asyncio.gather(forwarder._process(forward_id), forwarder._process(forward_id))
    assert sent == [str(forward_id)]


async def test_a_lost_notification_is_recovered_by_the_poll(
    replicas: Replicas,
) -> None:
    replicas.network.dropped.add(replicas.hub_a.instance_id)
    session_id = await _new_session(replicas.client_b)
    posted = await _message(replicas.client_b, session_id, "hello")
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "idle"


async def test_cancel_to_a_stale_replica_is_an_error(
    replicas: Replicas, replica_harness: FakeHarness
) -> None:
    replica_harness.hold = True
    session_id = await _new_session(replicas.client_b)
    task = asyncio.create_task(_message(replicas.client_b, session_id, "go"))
    await _in_progress(replicas, session_id)
    worker_id, _lease_id = await _lease(replicas.store, session_id)
    async with replicas.store.session() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker_id)
            .values(last_seen=utc_now() - timedelta(minutes=5))
        )
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 503, cancelled.text
    assert cancelled.json()["error"]["code"] == "worker_unreachable"
    replica_harness.hold = False
    task.cancel()
