import uuid
from datetime import timedelta
from typing import Any

import pytest

from apipi.services.ingest import IngestBatcher, flush_batch, last_seq_for
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    list_items,
    list_turns,
    set_session_lease,
)
from apipi.worker.protocol import WorkerEnvelope


async def _leased(
    store: Store, worker_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )
        lease_id = uuid.uuid4()
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
        return tenant.id, row.id, lease_id


def _envelope(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> WorkerEnvelope:
    raw_turn = payload.get("turn_id")
    data_turn = None
    if isinstance(payload.get("data"), dict):
        maybe = payload["data"].get("turn_id")
        data_turn = maybe if isinstance(maybe, str) else None
    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": raw_turn if isinstance(raw_turn, str) else data_turn,
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )


def _turn_flow(session_id: uuid.UUID, turn_id: uuid.UUID) -> list[WorkerEnvelope]:
    item_id = uuid.uuid4()
    return [
        _envelope(session_id, 1, "session.status", {"status": "in_progress"}),
        _envelope(
            session_id,
            2,
            "event",
            {"type": "agent.session.in_progress", "data": {}},
        ),
        _envelope(
            session_id, 3, "turn.status", {"turn_id": str(turn_id), "status": "started"}
        ),
        _envelope(
            session_id,
            4,
            "event",
            {
                "type": "agent.session.turn.created",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        _envelope(
            session_id,
            5,
            "event",
            {
                "type": "agent.session.turn.in_progress",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        _envelope(
            session_id,
            6,
            "item.added",
            {
                "item_id": str(item_id),
                "item_type": "message",
                "turn_id": str(turn_id),
                "data": {"role": "user", "content": "hi"},
            },
        ),
        _envelope(
            session_id,
            7,
            "event",
            {
                "type": "agent.session.turn.item.added",
                "data": {
                    "item_id": str(item_id),
                    "item_type": "message",
                    "turn_id": str(turn_id),
                },
                "turn_id": str(turn_id),
            },
        ),
        _envelope(
            session_id,
            8,
            "event",
            {
                "type": "agent.session.turn.item.done",
                "data": {"item_id": str(item_id), "turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        _envelope(
            session_id,
            9,
            "turn.status",
            {"turn_id": str(turn_id), "status": "completed"},
        ),
        _envelope(
            session_id,
            10,
            "usage",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "tool_names": ["get_weather"],
                "tool_counts": {"get_weather": 1},
            },
        ),
        _envelope(
            session_id,
            11,
            "event",
            {
                "type": "agent.session.turn.completed",
                "data": {"turn_id": str(turn_id)},
                "turn_id": str(turn_id),
            },
        ),
        _envelope(session_id, 12, "session.status", {"status": "idle"}),
        _envelope(session_id, 13, "event", {"type": "agent.session.idle", "data": {}}),
    ]


async def _flush(
    store: Store,
    worker_id: uuid.UUID,
    envelopes: list[WorkerEnvelope],
    settings: Any = None,
):
    batcher = IngestBatcher()
    for envelope in envelopes:
        batcher.add(envelope, 128)
    return await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )


async def test_full_turn_flow_applies_once(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    flow = _turn_flow(session_id, turn_id)
    outcome = await _flush(store, worker_id, flow, settings)
    assert outcome.acks == {session_id: 13}
    assert outcome.rejected == []
    assert len(outcome.wakes) == 7
    async with store.session() as db:
        turns = await list_turns(db, tenant_id, session_id)
        assert turns is not None and len(turns) == 1
        assert turns[0].id == turn_id
        assert turns[0].status == "completed"
        assert turns[0].usage is not None and turns[0].usage["prompt_tokens"] == 11
        items = await list_items(db, tenant_id, session_id)
        assert items is not None and len(items) == 1
        assert items[0].turn_id == turn_id
        events = await list_events(db, tenant_id, session_id)
        types = [event.type for event in events]
        assert types == [
            "agent.session.in_progress",
            "agent.session.turn.created",
            "agent.session.turn.in_progress",
            "agent.session.turn.item.added",
            "agent.session.turn.item.done",
            "agent.session.turn.completed",
            "agent.session.idle",
        ]
        from datetime import date

        from apipi.store.repo import get_session, get_turn_log, usage_day

        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        assert row.worker_seq == 13
        turn_log = await get_turn_log(db, tenant_id, turn_id)
        assert turn_log is not None
        assert turn_log.status == "completed"
        assert turn_log.tool_names == ["get_weather"]
        totals = await usage_day(db, tenant_id, date.today())
        assert totals["prompt_tokens"] == 11
        assert totals["turns"] == 1
    assert await last_seq_for(store, worker_id, session_id) == 13


async def test_duplicate_batch_is_noop(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    flow = _turn_flow(session_id, turn_id)
    first = await _flush(store, worker_id, flow, settings)
    second = await _flush(store, worker_id, flow, settings)
    assert first.acks == second.acks == {session_id: 13}
    assert second.rejected == []
    assert second.wakes == []


async def test_envelope_without_lease_is_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )
        session_id = row.id
    outcome = await _flush(
        store,
        worker_id,
        [_envelope(session_id, 1, "session.status", {"status": "idle"})],
        settings,
    )
    assert outcome.acks == {session_id: 1}
    assert [reason for _, _, reason in outcome.rejected] == ["not_leased"]
    assert outcome.wakes == []


async def test_turn_mismatch_is_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    first_turn = uuid.uuid4()
    await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "turn.status",
                {"turn_id": str(first_turn), "status": "started"},
            )
        ],
        settings,
    )
    other_turn = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                2,
                "turn.status",
                {"turn_id": str(other_turn), "status": "started"},
            ),
            _envelope(
                session_id,
                3,
                "event",
                {
                    "type": "agent.session.turn.retrying",
                    "data": {"turn_id": str(other_turn)},
                    "turn_id": str(other_turn),
                },
            ),
        ],
        settings,
    )
    assert outcome.acks == {session_id: 3}
    assert [reason for _, _, reason in outcome.rejected] == [
        "turn_mismatch",
        "turn_mismatch",
    ]
    assert await last_seq_for(store, worker_id, session_id) == 3


async def test_unknown_and_live_events_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id, 1, "event", {"type": "agent.session.nope", "data": {}}
            ),
            _envelope(
                session_id,
                2,
                "event",
                {"type": "agent.session.turn.output_text.delta", "data": {}},
            ),
        ],
        settings,
    )
    assert [reason for _, _, reason in outcome.rejected] == [
        "unknown_event",
        "live_event",
    ]
    assert outcome.wakes == []
    assert outcome.acks == {session_id: 2}


async def test_deferred_types_rejected_without_apply(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "sandbox.status",
                {"status": "ready"},
            ),
            _envelope(
                session_id,
                2,
                "artifact.completed",
                {"artifact_id": str(uuid.uuid4())},
            ),
        ],
        settings,
    )
    assert [reason for _, _, reason in outcome.rejected] == [
        "not_implemented",
        "invalid_envelope",
    ]
    assert outcome.acks == {session_id: 2}


async def test_oversize_envelope_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    _tenant, session_id, _lease = await _leased(store, worker_id)
    batcher = IngestBatcher()
    batcher.add(
        _envelope(session_id, 1, "session.status", {"status": "idle"}),
        2 * 1024 * 1024,
    )
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert [reason for _, _, reason in outcome.rejected] == ["oversize"]
    assert outcome.acks == {session_id: 1}


async def test_failed_turn_records_failure(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "turn.status",
                {"turn_id": str(turn_id), "status": "started"},
            ),
            _envelope(
                session_id,
                2,
                "turn.status",
                {
                    "turn_id": str(turn_id),
                    "status": "failed",
                    "code": "worker_outbox_full",
                    "message": "Worker outbox is full",
                },
            ),
            _envelope(
                session_id,
                3,
                "usage",
                {
                    "turn_id": str(turn_id),
                    "status": "failed",
                    "prompt_tokens": 5,
                    "completion_tokens": 0,
                    "error_code": "worker_outbox_full",
                    "failure": {
                        "message": "Worker outbox is full",
                        "code": "worker_outbox_full",
                        "failure_source": "internal",
                        "retryable": False,
                        "upstream_status": None,
                        "legacy_code": None,
                        "upstream_attempts": None,
                    },
                },
            ),
            _envelope(
                session_id,
                4,
                "event",
                {
                    "type": "agent.session.turn.failed",
                    "data": {"turn_id": str(turn_id), "code": "worker_outbox_full"},
                    "turn_id": str(turn_id),
                },
            ),
        ],
        settings,
    )
    assert outcome.rejected == []
    assert outcome.acks == {session_id: 4}
    async with store.session() as db:
        from apipi.store.repo import get_session_turn, get_turn_log

        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        assert turn is not None and turn.status == "failed"
        turn_log = await get_turn_log(db, tenant_id, turn_id)
        assert turn_log is not None and turn_log.error_code == "worker_outbox_full"


async def test_mid_apply_failure_rolls_back_envelope(
    store: Store, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.services.runtime as runtime_module

    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    turn_id = uuid.uuid4()

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("turn log store down")

    monkeypatch.setattr(runtime_module, "_write_turn_log", boom)
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "turn.status",
                {"turn_id": str(turn_id), "status": "started"},
            ),
            _envelope(
                session_id,
                2,
                "usage",
                {
                    "turn_id": str(turn_id),
                    "status": "completed",
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                },
            ),
            _envelope(session_id, 3, "session.status", {"status": "idle"}),
        ],
        settings,
    )
    assert [reason for _, _, reason in outcome.rejected] == ["ingest_error"]
    assert outcome.acks == {session_id: 3}
    assert outcome.wakes == []
    async with store.session() as db:
        from apipi.store.repo import get_session, get_session_turn, get_turn_log

        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        assert turn is not None
        assert turn.usage is None
        assert await get_turn_log(db, tenant_id, turn_id) is None
        row = await get_session(db, tenant_id, session_id)
        assert row is not None and row.status == "idle"
        assert row.worker_seq == 3


async def test_item_done_merges_data_without_public_event(
    store: Store, settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    turn_id = uuid.uuid4()
    item_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "turn.status",
                {"turn_id": str(turn_id), "status": "started"},
            ),
            _envelope(
                session_id,
                2,
                "item.added",
                {
                    "item_id": str(item_id),
                    "item_type": "message",
                    "turn_id": str(turn_id),
                    "data": {"role": "user"},
                },
            ),
            _envelope(
                session_id,
                3,
                "item.done",
                {
                    "item_id": str(item_id),
                    "turn_id": str(turn_id),
                    "data": {"content": "hi"},
                },
            ),
            _envelope(
                session_id,
                4,
                "item.done",
                {"item_id": str(item_id), "turn_id": str(turn_id)},
            ),
        ],
        settings,
    )
    assert outcome.rejected == []
    assert outcome.wakes == []
    async with store.session() as db:
        from apipi.store.repo import get_item

        item = await get_item(db, tenant_id, item_id)
        assert item is not None
        assert item.data == {"role": "user", "content": "hi"}
        assert await list_events(db, tenant_id, session_id) == []


async def test_item_done_for_other_turn_is_rejected(store: Store, settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, _lease = await _leased(store, worker_id)
    first_turn = uuid.uuid4()
    other_turn = uuid.uuid4()
    item_id = uuid.uuid4()
    outcome = await _flush(
        store,
        worker_id,
        [
            _envelope(
                session_id,
                1,
                "turn.status",
                {"turn_id": str(first_turn), "status": "started"},
            ),
            _envelope(
                session_id,
                2,
                "item.added",
                {
                    "item_id": str(item_id),
                    "item_type": "message",
                    "turn_id": str(first_turn),
                    "data": {},
                },
            ),
            _envelope(
                session_id,
                3,
                "turn.status",
                {"turn_id": str(first_turn), "status": "completed"},
            ),
            _envelope(
                session_id,
                4,
                "turn.status",
                {"turn_id": str(other_turn), "status": "started"},
            ),
            _envelope(
                session_id,
                5,
                "item.done",
                {
                    "item_id": str(item_id),
                    "turn_id": str(first_turn),
                    "data": {"content": "hi"},
                },
            ),
        ],
        settings,
    )
    assert [reason for _, _, reason in outcome.rejected] == ["turn_mismatch"]
    assert outcome.acks == {session_id: 5}
    async with store.session() as db:
        from apipi.store.repo import get_item

        item = await get_item(db, tenant_id, item_id)
        assert item is not None
        assert item.data == {}


def test_batcher_full_and_window() -> None:
    batcher = IngestBatcher(max_messages=2, max_bytes=10 * 1024 * 1024)
    session_id = uuid.uuid4()
    assert batcher.poll_timeout(0.05) is None
    assert batcher.should_flush(0.05) is False
    batcher.add(_envelope(session_id, 1, "session.status", {"status": "idle"}), 10)
    assert batcher.poll_timeout(0.05) is not None
    assert batcher.should_flush(0.05) is False
    batcher.add(_envelope(session_id, 2, "session.status", {"status": "idle"}), 10)
    assert batcher.full() is True
    assert batcher.should_flush(0.05) is True
    assert len(batcher.take()) == 2
    assert len(batcher) == 0
