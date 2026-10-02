import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from apipi.config import Settings
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.services.runtime import (
    EventHub,
    FakeHarness,
    Harness,
    _emit_item,
    _fail_turn,
    continue_turn,
    run_turn,
)
from apipi.services.sink import DirectSink, OutboxSink
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    get_session,
    get_session_turn,
    list_items,
    list_turns,
)
from apipi.worker.outbox import Outbox
from apipi.worker.protocol import WorkerEnvelope


class NoWriteStore(Store):
    """A store whose sessions reject every write; reads pass through."""

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        async with super().session() as db:
            yield _NoWriteSession(db)


class _NoWriteSession:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in {"add", "flush", "delete"}:
            raise AssertionError(f"unexpected database write: {name}")
        return getattr(self._inner, name)


async def _session(store: Store) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, model="m1", status="idle", environment={"type": "none"}
        )
        return tenant.id, row.id


def _sink(outbox: Outbox, tenant_id: uuid.UUID, session_id: uuid.UUID) -> OutboxSink:
    return OutboxSink(outbox, tenant_id, session_id)


async def test_outbox_sink_buffers_without_db_writes(store: Store) -> None:
    tenant_id, session_id = await _session(store)
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    async with store.session() as db:
        turn_id = await sink.create_turn(
            db, tenant_id, session_id, status="in_progress"
        )
        await _emit_item(
            db,
            EventHub(),
            tenant_id,
            session_id,
            turn_id=turn_id,
            type="message",
            data={"role": "user", "content": "hi"},
            sink=sink,
        )
        await _fail_turn(
            db,
            EventHub(),
            tenant_id,
            session_id,
            turn_id,
            "boom",
            code="spawn_failed",
            sink=sink,
        )
    kinds = [(item["type"], item["seq"]) for item in outbox.pending(session_id)]
    assert [kind for kind, _ in kinds] == [
        "turn.status",
        "item.added",
        "event",
        "event",
        "turn.status",
        "usage",
        "event",
        "event",
        "session.status",
        "event",
    ]
    assert [seq for _, seq in kinds] == list(range(1, 11))
    async with store.session() as db:
        assert await list_turns(db, tenant_id, session_id) == []
        assert await list_items(db, tenant_id, session_id) == []
        assert await list_events(db, tenant_id, session_id) == []


async def test_outbox_run_performs_no_db_writes(
    store: Store, settings: Settings
) -> None:
    nowrite = NoWriteStore(store.engine)
    tenant_id, session_id = await _session(store)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    await run_turn(
        nowrite,  # type: ignore[arg-type]
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        sink=_sink(outbox, tenant_id, session_id),
    )
    assert len(outbox.pending(session_id)) > 0
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        assert await list_turns(db, tenant_id, session_id) == []
        assert await list_events(db, tenant_id, session_id) == []


async def test_outbox_run_matches_direct_run(store: Store, settings: Settings) -> None:
    direct_tenant, direct_session = await _session(store)
    outbox_tenant, outbox_session = await _session(store)

    def harness() -> FakeHarness:
        made = FakeHarness()
        made.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
        return made

    await run_turn(
        store,
        EventHub(),
        cast(Harness, harness()),
        direct_tenant,
        direct_session,
        "hello",
        settings=settings,
    )
    outbox = Outbox()
    await run_turn(
        store,
        EventHub(),
        cast(Harness, harness()),
        outbox_tenant,
        outbox_session,
        "hello",
        settings=settings,
        sink=_sink(outbox, outbox_tenant, outbox_session),
    )
    batcher = IngestBatcher()
    for envelope in outbox.pending(outbox_session):
        batcher.add(WorkerEnvelope.model_validate(envelope), 128)
    worker_id = uuid.uuid4()
    async with store.session() as db:
        from datetime import timedelta

        from apipi.store.models import utc_now
        from apipi.store.repo import set_session_lease

        await set_session_lease(
            db,
            outbox_tenant,
            outbox_session,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert outcome.rejected == []

    async def snapshot(tenant_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, Any]:
        async with store.session() as db:
            events = await list_events(db, tenant_id, session_id)
            items = await list_items(db, tenant_id, session_id)
            turns = await list_turns(db, tenant_id, session_id)
            assert turns is not None and len(turns) == 1
            from apipi.store.repo import get_turn_log

            turn_log = await get_turn_log(db, tenant_id, turns[0].id)
            assert turn_log is not None
            return {
                "events": [(event.type, _canon(event.data)) for event in events],
                "items": [(item.type, _canon(item.data)) for item in (items or [])],
                "turn": (turns[0].status, _canon(turns[0].usage)),
                "log": (
                    turn_log.status,
                    turn_log.prompt_tokens,
                    turn_log.tool_names,
                    turn_log.mcp_names,
                ),
            }

    direct = await snapshot(direct_tenant, direct_session)
    replayed = await snapshot(outbox_tenant, outbox_session)
    assert direct["events"] == replayed["events"]
    assert direct["items"] == replayed["items"]
    assert direct["turn"] == replayed["turn"]
    assert direct["log"] == replayed["log"]


def _canon(value: Any) -> Any:
    """Replace UUID strings with stable placeholders for comparisons."""
    if isinstance(value, str):
        try:
            uuid.UUID(value)
        except ValueError:
            return value
        return "UUID"
    if isinstance(value, dict):
        return {key: _canon(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canon(item) for item in value]
    return value


async def test_continued_turn_tally_accumulates(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _session(store)
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    first = FakeHarness()
    first.function_calls = [{"call_id": "c1", "name": "first_tool", "arguments": {}}]
    await run_turn(
        store,
        EventHub(),
        cast(Harness, first),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        sink=sink,
    )
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
    pending_status = [
        item["payload"].get("status")
        for item in outbox.pending(session_id)
        if item["type"] == "session.status"
    ]
    assert pending_status[-1] == "requires_action"
    tools, counts, _mcps, _mcp_counts = sink.tally_snapshot()
    assert tools == ["first_tool"]
    assert counts == {"first_tool": 1}


async def test_outbox_full_fails_turn_with_code(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _session(store)
    outbox = Outbox(max_messages=10)
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": f"c{n}", "name": f"tool_{n}"} for n in range(3)]
    await run_turn(
        store,
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        sink=_sink(outbox, tenant_id, session_id),
    )
    failed = [
        item
        for item in outbox.pending(session_id)
        if item["type"] == "turn.status" and item["payload"].get("status") == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["payload"]["code"] == "worker_outbox_full"
    usages = [item for item in outbox.pending(session_id) if item["type"] == "usage"]
    assert len(usages) == 1
    assert usages[0]["payload"]["error_code"] == "worker_outbox_full"
    assert usages[0]["payload"]["failure"]["code"] == "worker_outbox_full"


async def test_outbox_run_with_context_writes_no_db(
    store: Store, settings: Settings
) -> None:
    from apipi.services.turn_context import build_turn_context

    tenant_id, session_id = await _session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    nowrite = NoWriteStore(store.engine)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    await run_turn(
        nowrite,  # type: ignore[arg-type]
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
        sink=_sink(outbox, tenant_id, session_id),
    )
    assert len(outbox.pending(session_id)) > 0
    worker_id = uuid.uuid4()
    async with store.session() as db:
        from datetime import timedelta

        from apipi.store.models import utc_now
        from apipi.store.repo import set_session_lease

        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    batcher = IngestBatcher()
    for envelope in outbox.pending(session_id):
        batcher.add(WorkerEnvelope.model_validate(envelope), 128)
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert outcome.rejected == []
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        turns = await list_turns(db, tenant_id, session_id)
        assert turns is not None and len(turns) == 1
        assert turns[0].status == "completed"


async def test_direct_sink_matches_repo_calls(store: Store) -> None:
    tenant_id, session_id = await _session(store)
    sink = DirectSink()
    async with store.session() as db:
        turn_id = await sink.create_turn(
            db, tenant_id, session_id, status="in_progress"
        )
        item_id = await sink.create_item(
            db, tenant_id, session_id, type="message", turn_id=turn_id, data={}
        )
        assert isinstance(turn_id, uuid.UUID)
        assert isinstance(item_id, uuid.UUID)
        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        assert turn is not None


async def test_fail_turn_without_sink_keeps_direct_writes(store: Store) -> None:
    tenant_id, session_id = await _session(store)
    async with store.session() as db:
        turn = await create_turn(db, tenant_id, session_id, status="in_progress")
        await _fail_turn(
            db, EventHub(), tenant_id, session_id, turn.id, "boom", code="spawn_failed"
        )
        events = await list_events(db, tenant_id, session_id)
    assert "agent.session.turn.failed" in [event.type for event in events]


async def test_continue_turn_routes_through_sink(
    store: Store, settings: Settings
) -> None:
    from datetime import timedelta

    from apipi.store.models import utc_now
    from apipi.store.repo import set_session_lease

    tenant_id, session_id = await _session(store)
    worker_id = uuid.uuid4()
    async with store.session() as db:
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    first = FakeHarness()
    first.function_calls = [{"call_id": "c1", "name": "tool_a", "arguments": {}}]
    await run_turn(
        store,
        EventHub(),
        cast(Harness, first),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        sink=sink,
    )
    started = next(
        item
        for item in outbox.pending(session_id)
        if item["type"] == "turn.status" and item["payload"].get("status") == "started"
    )
    turn_id = uuid.UUID(str(started["payload"]["turn_id"]))
    batcher = IngestBatcher()
    for envelope in outbox.pending(session_id):
        batcher.add(WorkerEnvelope.model_validate(envelope), 128)
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert outcome.rejected == []
    outbox.acked(session_id, outcome.acks[session_id])
    await continue_turn(
        store,
        EventHub(),
        cast(Harness, FakeHarness()),
        tenant_id,
        session_id,
        turn_id=turn_id,
        call_id="c1",
        success=True,
        output="done",
        error=None,
        settings=settings,
        sink=sink,
    )
    leg_two = IngestBatcher()
    for envelope in outbox.pending(session_id):
        leg_two.add(WorkerEnvelope.model_validate(envelope), 128)
    outcome = await flush_batch(
        store, leg_two.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert outcome.rejected == []
    async with store.session() as db:
        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        assert turn is not None and turn.status == "completed"
        from apipi.store.repo import get_turn_log

        turn_log = await get_turn_log(db, tenant_id, turn_id)
        assert turn_log is not None
        assert turn_log.tool_names == ["tool_a"]
        events = await list_events(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert "agent.session.requires_action" in types
    assert "agent.session.turn.completed" in types
