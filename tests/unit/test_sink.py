import uuid
from typing import cast

import pytest
from tests.support.worker_turn import (
    ingest_outbox,
    lease_session,
    new_session,
)

from apipi.common.errors import ApiError
from apipi.common.event_bus import EventHub
from apipi.config import Settings
from apipi.services.ingest import DEFERRED_TYPES
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.repo import (
    get_session,
    get_session_turn,
    get_turn_log,
    list_items,
    list_turns,
)
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.outbox import Outbox
from apipi.worker.runtime import Harness, continue_turn, run_turn
from apipi.worker.sink import OutboxSink
from apipi.worker.turn_end import emit_item as _emit_item
from apipi.worker.turn_end import fail_turn as _fail_turn


def _sink(outbox: Outbox, tenant_id: uuid.UUID, session_id: uuid.UUID) -> OutboxSink:
    return OutboxSink(outbox, tenant_id, session_id)


async def test_outbox_sink_buffers_envelopes() -> None:
    tenant_id, session_id = uuid.uuid4(), uuid.uuid4()
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    turn_id = await sink.create_turn(tenant_id, session_id, status="in_progress")
    await _emit_item(
        EventHub(),
        tenant_id,
        session_id,
        turn_id=turn_id,
        type="message",
        data={"role": "user", "content": "hi"},
        sink=sink,
    )
    await _fail_turn(
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


async def test_outbox_run_writes_no_db_until_ingest(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    await run_turn(
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
    async with store.session() as db:
        assert await list_turns(db, tenant_id, session_id) == []
        assert await list_items(db, tenant_id, session_id) == []
        assert await list_events(db, tenant_id, session_id) == []
    worker_id = await lease_session(store, tenant_id, session_id)
    await ingest_outbox(
        store, settings, outbox, tenant_id, session_id, worker_id=worker_id
    )
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        turns = await list_turns(db, tenant_id, session_id)
        assert turns is not None and len(turns) == 1
        assert turns[0].status == "completed"
        turn_log = await get_turn_log(db, tenant_id, turns[0].id)
        assert turn_log is not None
        assert turn_log.mcp_names == ["mcp_tool"]


async def test_continued_turn_tally_accumulates(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    first = FakeHarness()
    first.function_calls = [{"call_id": "c1", "name": "first_tool", "arguments": {}}]
    await run_turn(
        EventHub(),
        cast(Harness, first),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
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
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox(max_messages=10, session_share=1.0)
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": f"c{n}", "name": f"tool_{n}"} for n in range(3)]
    await run_turn(
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
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


async def test_oversize_envelope_fails_turn_with_its_own_code(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c0", "name": "tool_" + "x" * 900}]
    monkeypatch.setattr("apipi.worker.outbox.MAX_MESSAGE_BYTES", 800)
    await run_turn(
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
        sink=_sink(outbox, tenant_id, session_id),
    )
    failed = [
        item
        for item in outbox.pending(session_id)
        if item["type"] == "turn.status" and item["payload"].get("status") == "failed"
    ]
    assert len(failed) == 1
    assert failed[0]["payload"]["code"] == "worker_message_too_large"


async def test_worker_emits_no_deferred_envelopes(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox()
    harness = FakeHarness()
    harness.mcp_calls = [{"call_id": "c1", "name": "mcp_tool"}]
    await run_turn(
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
        sink=_sink(outbox, tenant_id, session_id),
    )
    kinds = {item["type"] for item in outbox.pending(session_id)}
    assert kinds
    assert kinds.isdisjoint(DEFERRED_TYPES)


async def test_continue_turn_routes_through_sink(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    context = await build_turn_context(store, settings, tenant_id, session_id)
    worker_id = await lease_session(store, tenant_id, session_id)
    outbox = Outbox()
    sink = _sink(outbox, tenant_id, session_id)
    first = FakeHarness()
    first.function_calls = [{"call_id": "c1", "name": "tool_a", "arguments": {}}]
    await run_turn(
        EventHub(),
        cast(Harness, first),
        tenant_id,
        session_id,
        "hello",
        settings=settings,
        turn_context=context,
        sink=sink,
    )
    started = next(
        item
        for item in outbox.pending(session_id)
        if item["type"] == "turn.status" and item["payload"].get("status") == "started"
    )
    turn_id = uuid.UUID(str(started["payload"]["turn_id"]))
    outcome = await ingest_outbox(
        store, settings, outbox, tenant_id, session_id, worker_id=worker_id
    )
    outbox.acked(session_id, outcome.acks[session_id])
    waiting = await build_turn_context(store, settings, tenant_id, session_id)
    await continue_turn(
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
        turn_context=waiting,
        sink=sink,
    )
    await ingest_outbox(
        store, settings, outbox, tenant_id, session_id, worker_id=worker_id
    )
    async with store.session() as db:
        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        assert turn is not None and turn.status == "completed"
        turn_log = await get_turn_log(db, tenant_id, turn_id)
        assert turn_log is not None
        assert turn_log.tool_names == ["tool_a"]
        events = await list_events(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert "agent.session.requires_action" in types
    assert "agent.session.turn.completed" in types


async def test_run_without_turn_context_fails_loudly(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    with pytest.raises(ApiError):
        await run_turn(
            EventHub(),
            cast(Harness, FakeHarness()),
            tenant_id,
            session_id,
            "hello",
            settings=settings,
            sink=_sink(Outbox(), tenant_id, session_id),
        )
