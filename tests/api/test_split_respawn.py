import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from tests.support.http import auth
from tests.support.procs import fake_pi_shim
from tests.support.split_worker import (
    HeldRelease,
    split_client_for,
    worker_settings_for,
)

from apipi.config import CapacityError, Settings
from apipi.env.setup import SetupError
from apipi.protocol import LeaseRelease
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.store.repo import get_session_by_id
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.pi.harness import PiHarness


@dataclass
class _Run:
    statuses: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    hooked: list[bool] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    sandbox: list[str] = field(default_factory=list)
    leased: bool = False


def _message(text: str) -> dict[str, Any]:
    content = [{"type": "input_text", "text": text}]
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": content}],
            }
        ]
    }


async def _send(client: AsyncClient, token: str, session_id: str, text: str) -> str:
    response = await asyncio.wait_for(
        client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=auth(token),
            json=_message(text),
        ),
        timeout=15,
    )
    assert response.status_code == 200, response.json()
    return str(response.json()["status"])


def _reasons(sent: list[str], kind: str) -> list[str]:
    reasons: list[str] = []
    for raw in sent:
        message = json.loads(raw)
        payload = message.get("payload") or {}
        if message.get("type") == kind and payload.get("reason"):
            reasons.append(payload["reason"])
    return reasons


async def _two_turns(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    between: str,
    environment: str,
) -> _Run:
    run = _Run()
    sent: list[str] = []
    token = f"split-{between}-{environment}"
    worker_settings = worker_settings_for(
        settings.model_copy(update={"pi_command": str(fake_pi_shim(tmp_path))})
    )
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=worker_settings,
        sent=sent,
    ) as (_app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        real_kill = pool.kill
        real_hook = pool.on_kill
        assert real_hook is not None

        async def kill(session: Any, **kw: Any) -> None:
            run.killed.append(kw.get("reason", "session"))
            await real_kill(session, **kw)

        async def on_kill(session: uuid.UUID, proc: Any, release: bool) -> None:
            run.hooked.append(release)
            await real_hook(session, proc, release)

        async def sweep_dead() -> None:
            return None

        pool.kill = kill
        pool.on_kill = on_kill
        pool.sweep_dead = sweep_dead
        agent = await client.post(
            "/v1/agents",
            headers=auth(token),
            json={"name": "bot", "model": "test", "instructions": "one"},
        )
        assert agent.status_code == 200, agent.json()
        agent_id = agent.json()["id"]
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent_id, "environment": {"type": environment}},
        )
        assert created.status_code == 200, created.json()
        session_id = str(created.json()["id"])
        run.statuses.append(await _send(client, token, session_id, "first"))
        if between == "respawn":
            updated = await client.post(
                f"/v1/agents/{agent_id}",
                headers=auth(token),
                json={"instructions": "two"},
            )
            assert updated.status_code == 200, updated.json()
        else:
            proc = pool.peek(uuid.UUID(session_id))
            assert proc is not None
            await proc.terminate()
        run.statuses.append(await _send(client, token, session_id, "second"))
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events?limit=100", headers=auth(token)
        )
        run.types = [event["type"] for event in events.json()["data"]]
        run.leased = uuid.UUID(session_id) in worker.session_leases
    run.stops = _reasons(sent, "lifecycle.stop")
    run.sandbox = _reasons(sent, "sandbox.status")
    return run


@pytest.mark.parametrize("environment", ["none", "openai_hosted"])
@pytest.mark.parametrize("between", ["respawn", "crash"])
async def test_a_new_pi_at_turn_start_keeps_the_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    between: str,
    environment: str,
) -> None:
    run = await _two_turns(
        settings, store, worker_secret, tmp_path, between, environment
    )
    assert run.statuses == ["idle", "idle"], run.types
    assert run.types.count("agent.session.turn.completed") == 2, run.types
    assert "agent.session.error" not in run.types
    assert run.killed[0] == between
    assert run.hooked[0] is False
    assert run.stops[0] == between
    assert run.leased
    if environment == "openai_hosted":
        assert run.sandbox[0] == between


async def _session(client: AsyncClient, token: str, environment: str) -> str:
    agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={"name": "bot", "model": "test", "instructions": "one"},
    )
    assert agent.status_code == 200, agent.json()
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": environment}},
    )
    assert created.status_code == 200, created.json()
    return str(created.json()["id"])


async def _api_lease(store: Store, session_id: str) -> uuid.UUID | None:
    async with store.session() as db:
        row = await get_session_by_id(db, uuid.UUID(session_id))
    assert row is not None
    return row.lease_id


async def _api_released(store: Store, session_id: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while await _api_lease(store, session_id) is not None:
        assert loop.time() < deadline
        await asyncio.sleep(0.02)


async def _lease_settles(
    store: Store, worker: Any, session_id: str, *, leased: bool
) -> uuid.UUID | None:
    sid = uuid.UUID(session_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while True:
        api = await _api_lease(store, session_id)
        local = worker.session_leases.get(sid)
        if leased and api is not None and local == str(api):
            return api
        if not leased and api is None and local is None:
            return None
        assert loop.time() < deadline, (api, local)
        await asyncio.sleep(0.02)


async def _events(client: AsyncClient, token: str, session_id: str) -> list[Any]:
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events?limit=200", headers=auth(token)
    )
    return list(events.json()["data"])


def _failure_codes(events: list[Any]) -> list[str]:
    return [
        str(event["data"]["code"])
        for event in events
        if event["type"] == "agent.session.turn.failed"
    ]


def _pi_worker_settings(settings: Settings, tmp_path: Path) -> Settings:
    return worker_settings_for(
        settings.model_copy(update={"pi_command": str(fake_pi_shim(tmp_path))})
    )


async def _in_progress(app: Any, session_id: str) -> None:
    sid = uuid.UUID(session_id)
    queue = app.state.event_hub.subscribe(sid)
    try:
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=10)
            if event and event.get("type") == "agent.session.turn.in_progress":
                return
    finally:
        app.state.event_hub.unsubscribe(sid, queue)


@pytest.mark.parametrize("environment", ["none", "openai_hosted"])
async def test_a_memory_kill_during_a_turn_releases_the_lease_after_it(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    sent: list[str] = []
    token = f"split-memory-{environment}"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
        sent=sent,
    ) as (app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        real_hook = pool.on_kill
        assert real_hook is not None

        async def on_kill(session: uuid.UUID, proc: Any, release: bool) -> None:
            await real_hook(session, proc, release)
            if release:
                await _api_released(store, str(session))

        pool.on_kill = on_kill
        session_id = await _session(client, token, environment)
        assert await _send(client, token, session_id, "first") == "idle"
        held = asyncio.create_task(_send(client, token, session_id, "hold"))
        await _in_progress(app, session_id)
        before = len(sent)
        monkeypatch.setattr(
            "apipi.worker.procmem.read_group_rss_pss",
            lambda _pid, **_kw: (2 * 1024 * 1024, 0),
        )
        pool.settings = pool.settings.model_copy(update={"pi_mem_mib": 1})
        await pool.enforce_memory()
        pool.settings = pool.settings.model_copy(update={"pi_mem_mib": None})
        status = await held
        failed = _failure_codes(await _events(client, token, session_id))
        released = await _lease_settles(store, worker, session_id, leased=False)
        after = await _send(client, token, session_id, "third")
        types = [event["type"] for event in await _events(client, token, session_id)]
        kept = await _lease_settles(store, worker, session_id, leased=True)
    assert status == "idle"
    assert failed == ["pi_memory"]
    assert released is None
    assert after == "idle"
    assert types.count("agent.session.turn.completed") == 2, types
    assert kept is not None
    assert _reasons(sent[before:], "lifecycle.stop")[0] == "memory"
    if environment == "openai_hosted":
        assert _reasons(sent[before:], "sandbox.status")[0] == "memory"
        presigned = [
            json.loads(raw)["payload"].get("filename")
            for raw in sent[before:]
            if json.loads(raw).get("type") == "artifact.presign"
        ]
        assert "pi-session.jsonl" in presigned, presigned


@pytest.mark.parametrize("environment", ["none", "openai_hosted"])
async def test_a_pi_that_dies_before_get_is_swept_and_the_turn_goes_on(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    environment: str,
) -> None:
    sent: list[str] = []
    hooked: list[bool] = []
    token = f"split-sweep-{environment}"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
        sent=sent,
    ) as (_app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        real_get = pool.get
        real_hook = pool.on_kill
        assert real_hook is not None
        armed = False

        async def on_kill(session: uuid.UUID, proc: Any, release: bool) -> None:
            hooked.append(release)
            await real_hook(session, proc, release)

        async def get(session: uuid.UUID, **kw: Any) -> Any:
            nonlocal armed
            if armed:
                armed = False
                proc = pool.peek(session)
                assert proc is not None
                await proc.terminate()
                await pool.sweep_dead()
            return await real_get(session, **kw)

        pool.on_kill = on_kill
        pool.get = get
        session_id = await _session(client, token, environment)
        assert await _send(client, token, session_id, "first") == "idle"
        first = await _lease_settles(store, worker, session_id, leased=True)
        armed = True
        second = await _send(client, token, session_id, "second")
        kept = await _lease_settles(store, worker, session_id, leased=True)
        third = await _send(client, token, session_id, "third")
        types = [event["type"] for event in await _events(client, token, session_id)]
        stops = _reasons(sent, "lifecycle.stop")
        kills = list(hooked)
    assert second == "idle"
    assert third == "idle"
    assert types.count("agent.session.turn.completed") == 3, types
    assert "agent.session.error" not in types
    assert kills == [False]
    assert kept == first
    assert stops == ["crash"]


class _GatedCommands(asyncio.Queue[Any]):
    def __init__(self, op: str) -> None:
        super().__init__()
        self.op = op
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()
        self.parked: Any = None

    def _gated(self, message: Any) -> bool:
        text = message.get("text") if isinstance(message, dict) else None
        return isinstance(text, str) and json.loads(text).get("op") == self.op

    async def get(self) -> Any:
        while True:
            if self.parked is not None and self.gate.is_set():
                message, self.parked = self.parked, None
                return message
            if self.parked is None:
                message = await super().get()
                if not self._gated(message) or self.gate.is_set():
                    return message
                self.parked = message
                self.waiting.set()
                continue
            getter = asyncio.ensure_future(super().get())
            opener = asyncio.ensure_future(self.gate.wait())
            await asyncio.wait({getter, opener}, return_when=asyncio.FIRST_COMPLETED)
            opener.cancel()
            if getter.done():
                return getter.result()
            getter.cancel()


async def test_an_idle_release_that_crosses_a_turn_start_keeps_the_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
) -> None:
    token = "split-cross-wire"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (_app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        session_id = await _session(client, token, "none")
        sid = uuid.UUID(session_id)
        assert await _send(client, token, session_id, "first") == "idle"
        first = await _lease_settles(store, worker, session_id, leased=True)
        gated = _GatedCommands("turn.start")
        plain = worker._ws._outgoing
        worker._ws._outgoing = gated
        while not plain.empty():
            gated.put_nowait(plain.get_nowait())
        plain.put_nowait({"type": "websocket.noop"})
        second = asyncio.create_task(_send(client, token, session_id, "second"))
        await asyncio.wait_for(gated.waiting.wait(), timeout=10)
        await pool.kill(sid, reason="idle")
        assert sid not in worker.session_leases
        await asyncio.sleep(0.2)
        crossed = await _api_lease(store, session_id)
        gated.gate.set()
        status = await second
        kept = await _lease_settles(store, worker, session_id, leased=True)
        third = await _send(client, token, session_id, "third")
        types = [event["type"] for event in await _events(client, token, session_id)]
    assert crossed == first
    assert status == "idle"
    assert third == "idle"
    assert kept == first
    assert types.count("agent.session.turn.completed") == 3, types


async def test_an_idle_release_during_a_new_turn_keeps_the_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
) -> None:
    token = "split-cross-turn"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (_app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        session_id = await _session(client, token, "none")
        assert await _send(client, token, session_id, "first") == "idle"
        first = await _lease_settles(store, worker, session_id, leased=True)
        gate = asyncio.Event()
        real_note = execution.note_stopped
        assert real_note is not None

        async def note(session: uuid.UUID) -> None:
            await gate.wait()
            await real_note(session)

        execution.note_stopped = note
        killing = asyncio.create_task(pool.kill(uuid.UUID(session_id), reason="idle"))
        second = await _send(client, token, session_id, "second")
        gate.set()
        await killing
        execution.note_stopped = real_note
        kept = await _lease_settles(store, worker, session_id, leased=True)
        third = await _send(client, token, session_id, "third")
        types = [event["type"] for event in await _events(client, token, session_id)]
    assert second == "idle"
    assert third == "idle"
    assert kept == first
    assert types.count("agent.session.turn.completed") == 3, types


async def test_a_lease_release_ends_a_turn_in_progress(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.hold = True
    token = "split-release-in-progress"
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, worker):
        session_id = await _session(client, token, "none")
        sid = uuid.UUID(session_id)
        held = asyncio.create_task(_send(client, token, session_id, "go"))
        await _in_progress(app, session_id)
        lease_id = uuid.UUID(worker.session_leases[sid])
        await worker._ws.send_json(
            LeaseRelease(session_id=sid, lease_id=lease_id).to_wire()
        )
        status = await held
        failed = _failure_codes(await _events(client, token, session_id))
        lease = await _api_lease(store, session_id)
        await worker.execution.cancel(sid, status="cancelled")
    assert status == "idle"
    assert failed == ["turn_interrupted"]
    assert lease is None


async def _placed_memory(app: Any) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while True:
        placed = sum(
            sum(conn.lease_mem.values()) for conn in app.state.workers._conns.values()
        )
        if placed == 0 or loop.time() >= deadline:
            return placed
        await asyncio.sleep(0.02)


async def test_a_failed_boot_releases_its_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
) -> None:
    token = "split-boot-capacity"
    async with split_client_for(
        settings.model_copy(update={"sandbox_eager_boot": True}),
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (_app, client, worker):
        app = worker.app
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        real_get = pool.get
        boots: list[str] = []

        async def get(session: uuid.UUID, **kw: Any) -> Any:
            if not boots:
                boots.append("capacity")
                raise CapacityError("Too many live sessions")
            return await real_get(session, **kw)

        pool.get = get
        session_id = await _session(client, token, "openai_hosted")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while "agent.session.environment.failed" not in [
            event["type"] for event in await _events(client, token, session_id)
        ]:
            assert loop.time() < deadline
            await asyncio.sleep(0.02)
        released = await _lease_settles(store, worker, session_id, leased=False)
        placed = await _placed_memory(app)
        status = await _send(client, token, session_id, "next")
        kept = await _lease_settles(store, worker, session_id, leased=True)
    assert boots == ["capacity"]
    assert released is None
    assert placed == 0
    assert status == "idle"
    assert kept is not None


async def test_a_turn_that_fails_before_pi_starts_releases_its_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apipi.worker.runtime as runtime

    real_provision = runtime.provision_hosted_async
    failures: list[str] = []

    async def provision(*args: Any, **kw: Any) -> Any:
        if not failures:
            failures.append("setup")
            raise SetupError("Setup failed")
        return await real_provision(*args, **kw)

    monkeypatch.setattr(runtime, "provision_hosted_async", provision)
    token = "split-setup-failed"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (_app, client, worker):
        app = worker.app
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        session_id = await _session(client, token, "openai_hosted")
        failed = await _send(client, token, session_id, "first")
        released = await _lease_settles(store, worker, session_id, leased=False)
        placed = await _placed_memory(app)
        status = await _send(client, token, session_id, "second")
        kept = await _lease_settles(store, worker, session_id, leased=True)
    assert failures == ["setup"]
    assert failed == "failed"
    assert released is None
    assert placed == 0
    assert status == "idle"
    assert kept is not None


async def _boot(app: Any, store: Store, session_id: str) -> None:
    sid = uuid.UUID(session_id)
    async with store.session() as db:
        row = await get_session_by_id(db, sid)
    assert row is not None
    context = await build_turn_context(store, app.state.settings, row.tenant_id, sid)
    await app.state.execution.boot_hosted(row.tenant_id, sid, turn_context=context)


async def _post(client: AsyncClient, token: str, session_id: str, text: str) -> Any:
    return await asyncio.wait_for(
        client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=auth(token),
            json=_message(text),
        ),
        timeout=15,
    )


@pytest.mark.parametrize("request_kind", ["turn", "boot"])
async def test_a_request_during_a_lease_release_waits_and_gets_a_new_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    request_kind: str,
) -> None:
    token = f"split-release-window-{request_kind}"
    environment = "none" if request_kind == "turn" else "openai_hosted"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        session_id = await _session(client, token, environment)
        assert await _send(client, token, session_id, "first") == "idle"
        first = await _lease_settles(store, worker, session_id, leased=True)
        held = HeldRelease(app.state.workers)
        await execution.pool.kill(uuid.UUID(session_id), reason="idle")
        await asyncio.wait_for(held.entered.wait(), timeout=10)
        if request_kind == "turn":
            request = asyncio.create_task(_send(client, token, session_id, "second"))
        else:
            request = asyncio.create_task(_boot(app, store, session_id))
        await asyncio.wait_for(held.settling.wait(), timeout=10)
        waiting = not request.done()
        held.gate.set()
        result = await request
        kept = await _lease_settles(store, worker, session_id, leased=True)
        after = await _send(client, token, session_id, "third")
        types = [event["type"] for event in await _events(client, token, session_id)]
    assert waiting
    assert result == ("idle" if request_kind == "turn" else None)
    assert after == "idle"
    assert kept is not None and kept != first
    assert "agent.session.environment.failed" not in types
    assert "agent.session.turn.failed" not in types
    assert types.count("agent.session.turn.completed") == (
        3 if request_kind == "turn" else 2
    ), types


@pytest.mark.parametrize("request_kind", ["turn", "boot"])
async def test_a_release_that_never_finishes_fails_the_request_after_the_wait(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    request_kind: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    token = f"split-release-stuck-{request_kind}"
    environment = "none" if request_kind == "turn" else "openai_hosted"
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=_pi_worker_settings(settings, tmp_path),
    ) as (app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        hub = app.state.workers
        session_id = await _session(client, token, environment)
        assert await _send(client, token, session_id, "first") == "idle"
        first = await _lease_settles(store, worker, session_id, leased=True)
        hub.release_wait = 0.5
        held = HeldRelease(hub)
        await execution.pool.kill(uuid.UUID(session_id), reason="idle")
        await asyncio.wait_for(held.entered.wait(), timeout=10)
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = None
        if request_kind == "turn":
            response = await _post(client, token, session_id, "second")
        else:
            await asyncio.wait_for(_boot(app, store, session_id), timeout=15)
        elapsed = loop.time() - started
        events = await _events(client, token, session_id)
        stuck = await _api_lease(store, session_id)
        held.gate.set()
        await _api_released(store, session_id)
    assert 0.3 <= elapsed < 5, elapsed
    assert stuck == first
    timeouts = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "worker.lease.release_wait_timeout"
    ]
    assert timeouts, caplog.records
    if response is not None:
        assert response.status_code == 429, response.json()
        assert response.json()["error"]["code"] == "capacity"
    else:
        failed = [
            event["data"]
            for event in events
            if event["type"] == "agent.session.environment.failed"
        ]
        assert len(failed) == 1, events
        assert "No worker available" in json.dumps(failed[0])


async def test_a_cancel_during_a_lease_release_answers_like_an_idle_session(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.hold = True
    token = "split-release-cancel"
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, worker):
        session_id = await _session(client, token, "none")
        sid = uuid.UUID(session_id)
        held_turn = asyncio.create_task(_send(client, token, session_id, "go"))
        await _in_progress(app, session_id)
        lease_id = uuid.UUID(worker.session_leases[sid])
        held = HeldRelease(app.state.workers)
        await worker._ws.send_json(
            LeaseRelease(session_id=sid, lease_id=lease_id).to_wire()
        )
        await asyncio.wait_for(held.entered.wait(), timeout=10)
        cancel = asyncio.create_task(
            asyncio.wait_for(
                client.post(
                    f"/v1/agents/sessions/{session_id}/events",
                    headers=auth(token),
                    json={"type": "agent.session.input.cancel"},
                ),
                timeout=15,
            )
        )
        await asyncio.wait_for(held.settling.wait(), timeout=10)
        held.gate.set()
        cancelled = await cancel
        await held_turn
        lease = await _api_lease(store, session_id)
        await worker.execution.cancel(sid, status="cancelled")
    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["status"] != "in_progress"
    assert lease is None
