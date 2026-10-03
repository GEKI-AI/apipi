"""Split worker end to end without database credentials (#450).

The worker runs with `DATABASE_URL` unset, no object-store credentials,
and no `Store`: it executes turns from API-built contexts and reports
through the outbox, while the API ingests the envelopes. The pump below
stands in for the `/internal/worker` socket: presign replies go back to
the worker waiters and durable envelopes go through the API ingester.
"""

import asyncio
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from apipi.config import Settings
from apipi.protocol import WorkerEnvelope
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.store.engine import Store
from apipi.worker.artifact_upload import handle_presign_reply

pytestmark = pytest.mark.e2e

_PI_SESSION_SEED = '{"seed": "pi-session-bytes"}\n'


async def _split_settings(tmp_path: Path) -> Settings:
    return Settings(
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )


def _block_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("split worker must not construct storage clients")

    import apipi.store.blobs as blobs
    import apipi.store.engine as engine

    monkeypatch.setattr(engine, "create_engine", _boom)
    monkeypatch.setattr(engine, "Store", _boom)
    monkeypatch.setattr(blobs, "object_store", _boom)
    monkeypatch.setattr(blobs, "blob_store", _boom)
    monkeypatch.setattr(blobs, "S3Store", _boom)


class _Pump:
    """Drive presign replies and durable ingest for one worker outbox."""

    def __init__(self, store: Store, settings: Settings, worker_id: uuid.UUID) -> None:
        from apipi.store.blobs import object_store

        self._store = store
        self._settings = settings
        self._worker_id = worker_id
        # API-side object access for presign issue/verify. Built here,
        # before the test blocks storage constructors on the worker.
        self._objects = object_store(settings)
        self._seen: set[tuple[int, int]] = set()
        self.cursors: dict[uuid.UUID, int] = {}

    async def once(
        self, outbox: Any, session_id: uuid.UUID, waiters: dict[Any, Any]
    ) -> bool:
        progressed = False
        batcher = IngestBatcher()
        for item in outbox.pending(session_id):
            # Seq restarts at 1 for every outbox, so key flushed envelopes
            # by outbox as well as seq.
            key = (id(outbox), int(item["seq"]))
            if key in self._seen:
                continue
            self._seen.add(key)
            batcher.add(WorkerEnvelope.model_validate(item), 128)
            progressed = True
        if not len(batcher):
            return progressed
        queued = batcher.take()
        outcome = await flush_batch(
            self._store,
            queued,
            worker_id=self._worker_id,
            settings=self._settings,
            metrics=None,
            objects=self._objects,
        )
        assert outcome.rejected == []
        for reply in outcome.presign_replies:
            handle_presign_reply(waiters, reply)
        for acked_session, last_seq in outcome.acks.items():
            outbox.acked(acked_session, last_seq)
            known = self.cursors.get(acked_session, 0)
            self.cursors[acked_session] = max(known, last_seq)
        return True

    def adopt(self, outbox: Any, session_id: uuid.UUID) -> None:
        """Replay the restart handshake: adopt the API persisted cursor."""
        outbox.set_base(session_id, self.cursors.get(session_id, 0))


async def _drain_until(
    pump: _Pump,
    outbox: Any,
    session_id: uuid.UUID,
    waiters: dict[Any, Any],
    done: asyncio.Event,
) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        await pump.once(outbox, session_id, waiters)
        if done.is_set():
            await pump.once(outbox, session_id, waiters)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("split pump timed out")


def _worker_execution(
    settings: Settings,
    harness: Any,
    outbox: Any,
    hub: Any,
) -> Any:
    from apipi.worker.execution import LocalExecution
    from apipi.worker.pi.pool import PiPool

    return LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=harness,
        hub=hub,
        outbox=outbox,
    )


async def _hosted_session(
    store: Store, settings: Settings, workspace: Path
) -> tuple[uuid.UUID, uuid.UUID]:
    from apipi.store.repo import create_session, create_tenant

    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            model="m1",
            status="idle",
            environment={
                "type": "openai_hosted",
                "directory": str(workspace),
                "id": str(uuid.uuid4()),
            },
        )
        return tenant.id, row.id


async def test_split_turn_tools_artifacts_and_streaming(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.runtime import FakeHarness
    from apipi.services.turn_context import build_turn_context
    from apipi.store.models import utc_now
    from apipi.store.repo import (
        get_session,
        list_artifacts,
        list_events,
        list_turns,
        set_session_lease,
    )
    from apipi.worker.hub import dispatch_command
    from apipi.worker.outbox import Outbox

    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = await _split_settings(tmp_path)
    workspace = Path(settings.sessions_dir or "") / "tenant" / "session"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "outputs" / "notes.txt").write_text("field notes")
    from apipi.worker.pi.dirs import pi_session_file

    pi_session_file(workspace).parent.mkdir(parents=True, exist_ok=True)
    pi_session_file(workspace).write_text(_PI_SESSION_SEED)
    tenant_id, session_id = await _hosted_session(store, settings, workspace)
    worker_id = uuid.uuid4()
    async with store.session() as db:
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(minutes=10),
        )
    pump = _Pump(store, settings, worker_id)
    _block_storage(monkeypatch)

    outbox = Outbox()
    hub = InMemoryEventBus()
    live = hub.subscribe(session_id)
    harness = FakeHarness()
    harness.function_calls = [
        {"call_id": "call-1", "name": "read_notes", "arguments": {}}
    ]
    execution = _worker_execution(settings, harness, outbox, hub)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    assert context["pi_session"]["present"] is False
    done = asyncio.Event()

    async def _turn() -> None:
        try:
            await dispatch_command(
                execution,
                {
                    "op": "turn.start",
                    "session_id": str(session_id),
                    "payload": {
                        "tenant_id": str(tenant_id),
                        "text": "read the notes",
                        "context": context,
                    },
                },
            )
        finally:
            done.set()

    task = asyncio.create_task(_turn())
    try:
        await _drain_until(pump, outbox, session_id, execution.presign_waiters, done)
        await task
    finally:
        if not task.done():
            task.cancel()
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "requires_action"
        turns = await list_turns(db, tenant_id, session_id)
        assert turns is not None
        assert len(turns) == 1 and turns[0].status == "in_progress"

    follow_context = await build_turn_context(store, settings, tenant_id, session_id)
    turn_id = turns[0].id
    done.clear()
    follow_harness = FakeHarness()
    follow_harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    execution.harness = follow_harness

    async def _continue() -> None:
        try:
            await dispatch_command(
                execution,
                {
                    "op": "turn.continue",
                    "session_id": str(session_id),
                    "payload": {
                        "tenant_id": str(tenant_id),
                        "turn_id": str(turn_id),
                        "call_id": "call-1",
                        "success": True,
                        "output": "notes read",
                        "context": follow_context,
                    },
                },
            )
        finally:
            done.set()

    follow_task = asyncio.create_task(_continue())
    try:
        await _drain_until(pump, outbox, session_id, execution.presign_waiters, done)
        await follow_task
    finally:
        if not follow_task.done():
            follow_task.cancel()
    await pump.once(outbox, session_id, execution.presign_waiters)
    hub.unsubscribe(session_id, live)

    deltas = []
    while not live.empty():
        deltas.append(live.get_nowait())
    assert any(
        message.get("type") == "agent.session.turn.output_text.delta"
        for message in deltas
    ), "expected streamed deltas on the worker bus"

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        assert row.pi_session_id is not None, "pi session pointer must persist"
        turns = await list_turns(db, tenant_id, session_id)
        assert turns is not None
        assert len(turns) == 1 and turns[0].status == "completed"
        events = await list_events(db, tenant_id, session_id)
        types = [event.type for event in events]
        assert "agent.session.turn.completed" in types
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts, "workspace harvest must persist artifacts"


async def test_split_cold_restore_without_db(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.runtime import FakeHarness
    from apipi.services.turn_context import build_turn_context
    from apipi.store.models import utc_now
    from apipi.store.repo import get_session, set_session_lease
    from apipi.worker.hub import dispatch_command
    from apipi.worker.outbox import Outbox
    from apipi.worker.pi.dirs import pi_session_file

    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = await _split_settings(tmp_path)
    workspace = Path(settings.sessions_dir or "") / "tenant" / "session"
    workspace.mkdir(parents=True, exist_ok=True)

    pi_session_file(workspace).parent.mkdir(parents=True, exist_ok=True)
    pi_session_file(workspace).write_text(_PI_SESSION_SEED)
    tenant_id, session_id = await _hosted_session(store, settings, workspace)
    worker_id = uuid.uuid4()
    async with store.session() as db:
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(minutes=10),
        )
    pump = _Pump(store, settings, worker_id)
    _block_storage(monkeypatch)

    outbox = Outbox()
    execution = _worker_execution(settings, FakeHarness(), outbox, InMemoryEventBus())
    context = await build_turn_context(store, settings, tenant_id, session_id)
    done = asyncio.Event()

    async def _turn() -> None:
        try:
            await dispatch_command(
                execution,
                {
                    "op": "turn.start",
                    "session_id": str(session_id),
                    "payload": {
                        "tenant_id": str(tenant_id),
                        "text": "hello",
                        "context": context,
                    },
                },
            )
        finally:
            done.set()

    task = asyncio.create_task(_turn())
    try:
        await _drain_until(pump, outbox, session_id, execution.presign_waiters, done)
        await task
    finally:
        if not task.done():
            task.cancel()
    await pump.once(outbox, session_id, execution.presign_waiters)
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.pi_session_id is not None

    cold_outbox = Outbox()
    pump.adopt(cold_outbox, session_id)
    cold = _worker_execution(settings, FakeHarness(), cold_outbox, InMemoryEventBus())
    for path in (workspace / ".apipi", workspace / "notes.txt"):
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
        elif path.is_file():
            path.unlink()
    if pi_session_file(workspace).exists():
        pi_session_file(workspace).unlink()
    assert not pi_session_file(workspace).exists()
    cold_context = await build_turn_context(store, settings, tenant_id, session_id)
    assert cold_context["pi_session"]["present"] is True
    cold_done = asyncio.Event()

    async def _cold_turn() -> None:
        try:
            await dispatch_command(
                cold,
                {
                    "op": "turn.start",
                    "session_id": str(session_id),
                    "payload": {
                        "tenant_id": str(tenant_id),
                        "text": "follow-up",
                        "context": cold_context,
                    },
                },
            )
        finally:
            cold_done.set()

    cold_task = asyncio.create_task(_cold_turn())
    try:
        await _drain_until(
            pump, cold_outbox, session_id, cold.presign_waiters, cold_done
        )
        await cold_task
    finally:
        if not cold_task.done():
            cold_task.cancel()
    assert pi_session_file(workspace).read_text() == _PI_SESSION_SEED
