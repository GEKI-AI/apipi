"""Two API replicas, one store, one worker: commands cross replicas (#490).

The worker holds its socket on replica A. Every request goes to replica
B, which has no socket, so each command must be forwarded.
"""

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from tests.support.http import auth, post_message, tenant_of
from tests.support.notify_bus import NotifyNetwork
from tests.support.procs import fake_pi_shim
from tests.support.split_worker import (
    HeldRelease,
    SplitWorker,
    api_settings_for,
    spawn_split_worker,
    wait_for_idle,
    worker_settings_for,
)
from tests.support.waits import until

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthIdentity
from apipi.gateway.tokens import hash_token
from apipi.protocol import LeaseRelease
from apipi.services.turn_context import build_turn_context
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.models import SessionRow, WorkerForward, WorkerRow, utc_now
from apipi.store.repo import get_session
from apipi.worker.fake_harness import FakeHarness

TOKEN = "replicas"


def _credential(identity: AuthIdentity, bearer: str | None) -> str:
    return f"model:{identity.key_id}"


CREDENTIAL = f"model:{hash_token(TOKEN)}"
IMAGE = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"pixels" * 100).decode()


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
async def replicas(
    settings: Settings,
    store: Store,
    worker_secret: str,
    worker_harness: FakeHarness,
    tmp_path: Path,
) -> AsyncIterator[Replicas]:
    network = NotifyNetwork()
    api_settings = api_settings_for(settings).model_copy(
        update={"event_bus_fallback_poll": timedelta(milliseconds=100)}
    )
    app_a = create_app(
        api_settings.model_copy(update={"instance_id": "node-a"}),
        store=store,
        event_hub=network.bus(),
        model_credential=_credential,
    )
    app_b = create_app(
        api_settings.model_copy(update={"instance_id": "node-b"}),
        store=store,
        event_hub=network.bus(),
        model_credential=_credential,
    )
    for app in (app_a, app_b):
        await app.state.gateway.startup()
        _stop_loops(app)
    calls: list[tuple[str, str]] = []
    _spy(app_b, calls)
    worker = await spawn_split_worker(
        app_a,
        worker_settings_for(
            settings.model_copy(update={"pi_command": str(fake_pi_shim(tmp_path))})
        ),
        worker_harness,
        worker_secret,
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
        headers=auth(TOKEN),
        json={"name": "bot", "model": "test", **extra.pop("agent", {})},
    )
    assert agent.status_code == 200, agent.text
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(TOKEN),
        json={
            "agent_id": agent.json()["id"],
            "environment": environment or {"type": "none"},
            **extra,
        },
    )
    assert created.status_code == 200, created.text
    return uuid.UUID(created.json()["id"])


async def _finish_held_turn(
    replicas: Replicas, session_id: uuid.UUID, task: asyncio.Task[Any]
) -> None:
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    await asyncio.wait_for(task, timeout=20)


async def _in_progress(replicas: Replicas, session_id: uuid.UUID) -> None:
    async def running() -> bool:
        got = await replicas.client_b.get(
            f"/v1/agents/sessions/{session_id}", headers=auth(TOKEN)
        )
        return got.json()["status"] == "in_progress"

    await until(running, timeout=10.0)


async def _lease(store: Store, session_id: uuid.UUID) -> tuple[Any, Any]:
    async with store.session() as db:
        row = await get_session(db, tenant_of(TOKEN), session_id)
        assert row is not None
        return row.worker_id, row.lease_id


async def _forward_rows(store: Store) -> list[WorkerForward]:
    async with store.session() as db:
        return list(await db.scalars(select(WorkerForward)))


async def _no_forward_rows(store: Store) -> None:
    async def empty() -> bool:
        return await _forward_rows(store) == []

    await until(empty, timeout=10.0)


async def test_turn_start_is_forwarded_without_the_body_on_the_bus(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(replicas.client_b)
    text = "x" * 100_000
    posted = await post_message(replicas.client_b, TOKEN, session_id, text)
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
        f"/v1/agents/sessions/{session_id}/items", headers=auth(TOKEN)
    )
    assert text in str(items.json())


async def test_turn_cancel_is_forwarded(
    replicas: Replicas, worker_harness: FakeHarness
) -> None:
    worker_harness.hold = True
    session_id = await _new_session(replicas.client_b)
    task = asyncio.create_task(post_message(replicas.client_b, TOKEN, session_id, "go"))
    await _in_progress(replicas, session_id)
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    posted = await asyncio.wait_for(task, timeout=10)
    assert posted.status_code == 200
    assert posted.json()["status"] == "idle"
    assert ("command", "turn.cancel") in replicas.calls
    turns = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(TOKEN)
    )
    assert turns.json()["data"][0]["status"] == "cancelled"


async def test_session_stop_is_forwarded_and_waits(replicas: Replicas) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (
        await post_message(replicas.client_a, TOKEN, session_id, "hi")
    ).status_code == 200
    worker_id, lease_id = await _lease(replicas.store, session_id)
    deleted = await replicas.client_b.delete(
        f"/v1/agents/sessions/{session_id}", headers=auth(TOKEN)
    )
    assert deleted.status_code == 200, deleted.text
    assert ("command", "session.stop") in replicas.calls
    assert lease_id not in replicas.hub_a._conns[worker_id].leases
    assert session_id not in replicas.worker.session_leases
    await _no_forward_rows(replicas.store)
    gone = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}", headers=auth(TOKEN)
    )
    assert gone.status_code == 404


async def test_sandbox_boot_is_forwarded(replicas: Replicas) -> None:
    session_id = await _new_session(
        replicas.client_b, environment={"type": "openai_hosted"}
    )
    context = await build_turn_context(
        replicas.store, replicas.app_b.state.settings, tenant_of(TOKEN), session_id
    )
    await replicas.app_b.state.execution.boot_hosted(
        tenant_of(TOKEN), session_id, turn_context=context
    )
    assert replicas.calls == [("acquire", "sandbox.boot")]
    worker_id, lease_id = await _lease(replicas.store, session_id)
    assert worker_id is not None
    assert lease_id in replicas.hub_a._conns[worker_id].leases

    async def acked() -> bool:
        return len(replicas.hub_a.commands) == 0

    await until(acked, timeout=10.0)
    assert (await _lease(replicas.store, session_id))[1] == lease_id


async def test_a_forwarded_boot_that_fails_releases_its_lease(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(
        replicas.client_b, environment={"type": "openai_hosted"}
    )
    await replicas.app_b.state.execution.boot_hosted(tenant_of(TOKEN), session_id)
    assert replicas.calls == [("acquire", "sandbox.boot")]

    async def released() -> bool:
        _worker_id, lease_id = await _lease(replicas.store, session_id)
        return (
            lease_id is None
            and session_id not in replicas.worker.session_leases
            and not any(conn.lease_mem for conn in replicas.hub_a._conns.values())
        )

    await until(released, timeout=10.0)


@pytest.mark.parametrize("busy", [False, True])
async def test_a_forwarded_turn_during_a_lease_release_gets_a_new_lease(
    replicas: Replicas, worker_harness: FakeHarness, busy: bool
) -> None:
    worker_harness.hold = busy
    session_id = await _new_session(replicas.client_a)
    first = asyncio.create_task(
        post_message(replicas.client_a, TOKEN, session_id, "one")
    )
    if busy:
        await _in_progress(replicas, session_id)
    else:
        assert (await first).status_code == 200
    _worker_id, lease_id = await _lease(replicas.store, session_id)
    assert lease_id is not None
    held = HeldRelease(replicas.hub_a)
    await replicas.worker._ws.send_json(
        LeaseRelease(session_id=session_id, lease_id=lease_id).to_wire()
    )
    await asyncio.wait_for(held.entered.wait(), timeout=10)
    worker_harness.hold = False
    second = asyncio.create_task(
        post_message(replicas.client_b, TOKEN, session_id, "two")
    )
    await asyncio.wait_for(held.settling.wait(), timeout=10)
    held.gate.set()
    posted = await asyncio.wait_for(second, timeout=20)
    await asyncio.wait_for(first, timeout=20)
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "idle"
    assert replicas.calls == [
        ("command", "turn.cancel" if busy else "turn.start"),
        ("acquire", "turn.start"),
    ]
    _worker_id, new_lease = await _lease(replicas.store, session_id)
    assert new_lease is not None and new_lease != lease_id


async def test_a_forward_that_finds_another_lease_is_sent_on_it(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(replicas.client_a)
    first = await post_message(replicas.client_a, TOKEN, session_id, "one")
    assert first.status_code == 200, first.text
    _worker_id, lease_id = await _lease(replicas.store, session_id)
    assert lease_id is not None
    gone = uuid.uuid4()
    async with replicas.store.session() as db:
        await db.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(lease_id=gone)
        )
    forward = replicas.hub_b._forward_command

    async def restore(*args: Any, **kwargs: Any) -> Any:
        try:
            return await forward(*args, **kwargs)
        finally:
            async with replicas.store.session() as db:
                await db.execute(
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(lease_id=lease_id)
                )

    replicas.hub_b._forward_command = restore
    posted = await post_message(replicas.client_b, TOKEN, session_id, "two")
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "idle"
    assert replicas.calls == [("command", "turn.start"), ("command", "turn.start")]
    assert (await _lease(replicas.store, session_id))[1] == lease_id


async def test_lease_revoke_from_the_reaper_reaches_the_socket(
    replicas: Replicas,
) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (
        await post_message(replicas.client_a, TOKEN, session_id, "hi")
    ).status_code == 200
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

    await until(revoked, timeout=10.0)


async def test_cancel_that_cannot_reach_the_worker_is_an_error(
    replicas: Replicas, worker_harness: FakeHarness
) -> None:
    worker_harness.hold = True
    session_id = await _new_session(replicas.client_b)
    task = asyncio.create_task(post_message(replicas.client_b, TOKEN, session_id, "go"))
    await _in_progress(replicas, session_id)
    replicas.network.dropped.add(replicas.hub_a.instance_id)
    _stop_loops(replicas.app_a, "worker_forwards")
    replicas.hub_b.forwarder.sent_timeout = 0.3
    cancelled = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
        json={"type": "agent.session.input.cancel"},
    )
    assert cancelled.status_code == 504, cancelled.text
    assert cancelled.json()["error"]["code"] == "forward_timeout"
    await _no_forward_rows(replicas.store)
    replicas.network.dropped.clear()
    await _finish_held_turn(replicas, session_id, task)


async def _last_seen(store: Store, worker_id: uuid.UUID, last_seen: datetime) -> None:
    async with store.session() as db:
        await db.execute(
            update(WorkerRow)
            .where(WorkerRow.id == worker_id)
            .values(last_seen=last_seen)
        )


@pytest.mark.parametrize("busy", [False, True])
async def test_stale_replica_fails_fast_with_a_clear_code(
    replicas: Replicas, worker_harness: FakeHarness, busy: bool
) -> None:
    worker_harness.hold = busy
    session_id = await _new_session(replicas.client_a)
    first = asyncio.create_task(
        post_message(replicas.client_a, TOKEN, session_id, "hi")
    )
    if busy:
        await _in_progress(replicas, session_id)
    else:
        assert (await first).status_code == 200
    worker_id, _lease_id = await _lease(replicas.store, session_id)
    await _last_seen(replicas.store, worker_id, utc_now() - timedelta(minutes=5))
    started = asyncio.get_running_loop().time()
    follow = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
        json={"type": "agent.session.input.cancel"}
        if busy
        else {"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 503, follow.text
    assert follow.json()["error"]["code"] == "worker_unreachable"
    assert asyncio.get_running_loop().time() - started < 2
    assert replicas.network.sent == []
    if busy:
        await _last_seen(replicas.store, worker_id, utc_now())
        await _finish_held_turn(replicas, session_id, first)


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
        assert (
            await post_message(replicas.client_a, TOKEN, first_id, "one")
        ).status_code == 200
        assert (
            await post_message(replicas.client_a, TOKEN, second_id, "two")
        ).status_code == 200
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
    assert (
        await post_message(replicas.client_b, TOKEN, first_id, "one")
    ).status_code == 200
    second_id = await _new_session(replicas.client_b)
    full = await post_message(replicas.client_b, TOKEN, second_id, "two")
    assert full.status_code == 429, full.text
    assert full.json()["error"]["code"] == "capacity"
    await wait_for_idle(replicas.client_b, TOKEN, str(first_id))


async def test_a_forward_claimed_twice_is_sent_once(replicas: Replicas) -> None:
    session_id = await _new_session(replicas.client_a)
    assert (
        await post_message(replicas.client_a, TOKEN, session_id, "hi")
    ).status_code == 200
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
                tenant_id=tenant_of(TOKEN),
                session_id=session_id,
                worker_id=worker_id,
                target=replicas.hub_a.instance_id,
                origin=replicas.hub_b.instance_id,
                body={"payload": {"tenant_id": str(tenant_of(TOKEN))}},
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
    posted = await post_message(replicas.client_b, TOKEN, session_id, "hello")
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "idle"


async def test_every_forwarded_turn_carries_the_callback_credential(
    replicas: Replicas,
    worker_harness: FakeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(0)
    worker_harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    session_id = await _new_session(
        replicas.client_b,
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
    )
    first = await post_message(replicas.client_b, TOKEN, session_id, "use echo")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "requires_action"
    events = await replicas.client_b.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(TOKEN)
    )
    turn_id = next(
        event["data"]["turn_id"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    )
    resumed = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
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
    follow = await post_message(replicas.client_b, TOKEN, session_id, "again")
    assert follow.status_code == 200, follow.text
    assert follow.json()["status"] == "idle"
    assert replicas.calls == [
        ("acquire", "turn.start"),
        ("command", "turn.continue"),
        ("command", "turn.start"),
    ]
    assert worker_harness.api_keys == [CREDENTIAL] * 3
    assert CREDENTIAL not in caplog.text


async def test_forwarded_turn_without_a_credential_fails_at_once(
    replicas: Replicas, worker_harness: FakeHarness
) -> None:
    replicas.app_a.state.gateway.model_credentials.callback = None
    session_id = await _new_session(replicas.client_b)
    posted = await post_message(replicas.client_b, TOKEN, session_id, "go")
    assert posted.status_code == 503, posted.text
    assert posted.json()["error"]["code"] == "model_key_unavailable"
    assert worker_harness.api_keys == []
    await _no_forward_rows(replicas.store)


async def test_callback_that_raises_fails_the_turn_without_its_error_text(
    replicas: Replicas, worker_harness: FakeHarness
) -> None:
    def broken(identity: AuthIdentity, bearer: str | None) -> str:
        raise RuntimeError("hunter2")

    session_id = await _new_session(replicas.client_b)
    replicas.app_b.state.gateway.model_credentials.callback = broken
    posted = await post_message(replicas.client_b, TOKEN, session_id, "go")
    assert posted.status_code == 503, posted.text
    assert posted.json()["error"]["code"] == "model_key_unavailable"
    assert "hunter2" not in posted.text
    assert worker_harness.api_keys == []


async def _input_part(replicas: Replicas, kind: str) -> dict[str, Any]:
    if kind == "image":
        return {"type": "input_image", "image_url": f"data:image/png;base64,{IMAGE}"}
    upload = (
        ("notes.md", b"# notes", "text/markdown")
        if kind == "file"
        else ("report.xlsx", b"sheet", "application/octet-stream")
    )
    uploaded = await replicas.client_b.post(
        "/v1/files",
        headers=auth(TOKEN),
        data={"purpose": "user_data"},
        files={"file": upload},
    )
    assert uploaded.status_code == 200, uploaded.text
    part = {"type": "input_file", "file_id": uploaded.json()["id"]}
    if kind == "file":
        part["filename"] = "plan.md"
    return part


@pytest.mark.parametrize("kind", ["image", "file", "attachment"])
async def test_turn_start_with_an_input_part_is_forwarded_without_its_content(
    replicas: Replicas,
    worker_harness: FakeHarness,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from apipi.workerhub import forward as forward_module

    replicas.app_b.state.gateway.sessions.settings.model_registry = {
        "test": {"input": ["text", "image"]}
    }
    stored: list[dict[str, Any]] = []
    real = forward_module.create_worker_forward

    async def spy(db: Any, row: WorkerForward) -> None:
        stored.append(dict(row.body))
        await real(db, row)

    monkeypatch.setattr(forward_module, "create_worker_forward", spy)
    session_id = await _new_session(
        replicas.client_b, {"type": "openai_hosted"} if kind == "attachment" else None
    )
    part = await _input_part(replicas, kind)
    content = [{"type": "input_text", "text": "see"}, part]
    posted = await replicas.client_b.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(TOKEN),
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": content}],
                }
            ]
        },
    )
    assert posted.status_code == 200, posted.text
    assert replicas.calls == [("acquire", "turn.start")]
    payload = stored[0]["payload"]
    assert "context" not in payload
    assert payload["parts"][0] == {"type": "input_text", "text": "see"}
    forwarded = payload["parts"][1]
    if kind == "image":
        assert set(forwarded) == {"type", "file_id"}
        assert worker_harness.images == [
            [{"type": "image", "data": IMAGE, "mimeType": "image/png"}]
        ]
    elif kind == "file":
        assert forwarded == {
            "type": "file",
            "file_id": part["file_id"],
            "filename": "plan.md",
        }
        assert worker_harness.prompts == [
            'see\n<file name="plan.md">\n# notes\n</file>'
        ]
    else:
        assert forwarded == {
            "type": "file",
            "file_id": part["file_id"],
            "filename": "report.xlsx",
            "path": "attachments/report.xlsx",
        }
        assert worker_harness.prompts == [
            "see\nAttached: attachments/report.xlsx (xlsx, 5 B)"
        ]
        assert worker_harness.workspaces[-1]["attachments/report.xlsx"] == b"sheet"
