import uuid
from datetime import timedelta
from typing import Any

import pytest

from apipi.config import Settings
from apipi.protocol import WorkerEnvelope
from apipi.services.ingest import IngestBatcher, flush_batch, last_seq_for
from apipi.services.runtime import _write_turn_log
from apipi.services.usage import usage_event
from apipi.store.engine import Store
from apipi.store.errors import NotFoundError
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_item,
    create_session,
    create_tenant,
    create_turn,
    get_turn_log,
    list_items,
    record_search_usage,
    search_usage_for_turn,
    set_session_lease,
    usage_day,
    usage_totals,
)


async def _leased(
    store: Store, worker_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
        turn = await create_turn(db, tenant.id, row.id, status="in_progress")
        return tenant.id, row.id, turn.id


def _envelope(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> WorkerEnvelope:
    return WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": payload.get("turn_id"),
            "seq": seq,
            "type": type,
            "payload": payload,
        }
    )


async def _send_usage(
    store: Store,
    settings: Settings,
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> None:
    batcher = IngestBatcher()
    seq = (await last_seq_for(store, worker_id, session_id) or 0) + 1
    batcher.add(
        _envelope(
            session_id,
            seq,
            "usage",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        ),
        128,
    )
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=settings, metrics=None
    )
    assert outcome.rejected == []


async def _search(
    store: Store,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    provider: str = "tavily",
    key_source: str = "operator",
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
            key_source=key_source,
            calls=calls,
            units=units,
        )


async def _snapshot(
    store: Store, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> dict[str, Any]:
    async with store.session() as db:
        row = await get_turn_log(db, tenant_id, turn_id)
        assert row is not None
        day = await usage_day(db, tenant_id, utc_now().date())
        counts = await search_usage_for_turn(db, tenant_id, turn_id)
        return {
            "calls": row.search_calls,
            "units": row.search_units,
            "counts": row.search_counts,
            "day_calls": day["search_calls"],
            "day_units": day["search_units"],
            "day_turns": day["turns"],
            "table": counts,
        }


async def test_search_before_usage(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, turn_id, calls=1, units=2)
    await _search(store, tenant_id, session_id, turn_id, calls=1, units=2)
    await _send_usage(store, settings, worker_id, session_id, turn_id)
    snap = await _snapshot(store, tenant_id, turn_id)
    assert snap["calls"] == 2
    assert snap["units"] == 4
    assert snap["counts"] == {"tavily/operator": {"calls": 2, "units": 4}}
    assert snap["day_calls"] == 2
    assert snap["day_units"] == 4
    assert snap["day_turns"] == 1


async def test_usage_before_search(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _send_usage(store, settings, worker_id, session_id, turn_id)
    empty = await _snapshot(store, tenant_id, turn_id)
    assert empty["calls"] == 0
    assert empty["day_calls"] == 0
    await _search(store, tenant_id, session_id, turn_id, calls=1, units=2)
    await _search(store, tenant_id, session_id, turn_id, calls=1, units=2)
    snap = await _snapshot(store, tenant_id, turn_id)
    assert snap["calls"] == 2
    assert snap["units"] == 4
    assert snap["counts"] == {"tavily/operator": {"calls": 2, "units": 4}}
    assert snap["day_calls"] == 2
    assert snap["day_units"] == 4
    assert snap["day_turns"] == 1


async def test_orders_give_identical_totals(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, first = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, first, calls=3, units=5)
    await _send_usage(store, settings, worker_id, session_id, first)
    async with store.session() as db:
        second = (await create_turn(db, tenant_id, session_id, status="in_progress")).id
    await _send_usage(store, settings, worker_id, session_id, second)
    await _search(store, tenant_id, session_id, second, calls=3, units=5)
    one = await _snapshot(store, tenant_id, first)
    two = await _snapshot(store, tenant_id, second)
    for key in ("calls", "units", "counts", "table"):
        assert one[key] == two[key]
    assert two["day_calls"] == 6
    assert two["day_units"] == 10
    assert two["day_turns"] == 2


async def test_two_providers_in_one_turn(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, turn_id, "tavily", "operator", 2, 4)
    await _search(store, tenant_id, session_id, turn_id, "staan", "operator", 1, 1)
    await _send_usage(store, settings, worker_id, session_id, turn_id)
    await _search(store, tenant_id, session_id, turn_id, "staan", "tenant", 1, 1)
    await _search(store, tenant_id, session_id, turn_id, "tavily", "operator", 1, 2)
    snap = await _snapshot(store, tenant_id, turn_id)
    expected = {
        "staan/operator": {"calls": 1, "units": 1},
        "staan/tenant": {"calls": 1, "units": 1},
        "tavily/operator": {"calls": 3, "units": 6},
    }
    assert snap["counts"] == expected
    assert snap["table"] == (5, 8, expected)
    assert snap["calls"] == 5
    assert snap["units"] == 8
    assert snap["day_calls"] == 5
    assert snap["day_units"] == 8


async def test_rollup_totals_across_turns(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, first = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, first, calls=1, units=1)
    await _send_usage(store, settings, worker_id, session_id, first)
    async with store.session() as db:
        second = (await create_turn(db, tenant_id, session_id, status="in_progress")).id
    await _search(store, tenant_id, session_id, second, calls=2, units=4)
    await _send_usage(store, settings, worker_id, session_id, second)
    async with store.session() as db:
        day = await usage_day(db, tenant_id, utc_now().date())
        by_session = await usage_totals(db, tenant_id, session_id=session_id)
        by_turn = await usage_totals(db, tenant_id, turn_id=second)
    assert day["search_calls"] == 3
    assert day["search_units"] == 5
    assert day["turns"] == 2
    assert by_session["search_calls"] == 3
    assert by_session["search_units"] == 5
    assert by_turn["search_calls"] == 2
    assert by_turn["search_units"] == 4


async def test_turn_without_search_has_zero(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _send_usage(store, settings, worker_id, session_id, turn_id)
    snap = await _snapshot(store, tenant_id, turn_id)
    assert snap["calls"] == 0
    assert snap["units"] == 0
    assert snap["counts"] == {}
    assert snap["table"] == (0, 0, {})


async def test_usage_event_carries_search_fields(
    store: Store, settings: Settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, turn_id, calls=2, units=3)
    async with store.session() as db:
        calls, units, counts = await search_usage_for_turn(db, tenant_id, turn_id)
    event = usage_event(
        tenant_id=tenant_id,
        key_id="",
        session_id=session_id,
        turn_id=turn_id,
        agent_id=None,
        model=None,
        status="completed",
        latency_ms=0,
        usage={},
        tool_names=[],
        tool_counts={},
        mcp_names=[],
        mcp_counts={},
        environment_type="none",
        run_mode="none",
        instance_id=None,
        artifact_bytes=0,
        request_id=None,
        error_code=None,
        created_at=utc_now(),
        search_calls=calls,
        search_units=units,
        search_counts=counts,
    )
    assert event["search_calls"] == 2
    assert event["search_units"] == 3
    assert event["search_counts"] == {"tavily/operator": {"calls": 2, "units": 3}}
    plain = usage_event(
        tenant_id=tenant_id,
        key_id="",
        session_id=session_id,
        turn_id=turn_id,
        agent_id=None,
        model=None,
        status="completed",
        latency_ms=0,
        usage={},
        tool_names=[],
        tool_counts={},
        mcp_names=[],
        mcp_counts={},
        environment_type="none",
        run_mode="none",
        instance_id=None,
        artifact_bytes=0,
        request_id=None,
        error_code=None,
        created_at=utc_now(),
    )
    assert plain["search_calls"] == 0
    assert plain["search_units"] == 0
    assert plain["search_counts"] == {}


async def test_write_turn_log_emits_search_in_export_event(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, turn_id, calls=2, units=3)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apipi.services.runtime.export_usage",
        lambda _settings, _metrics, event: seen.append(event),
    )
    async with store.session() as db:
        await _write_turn_log(
            db, tenant_id, session_id, turn_id, status="completed", settings=settings
        )
    assert len(seen) == 1
    assert seen[0]["search_calls"] == 2
    assert seen[0]["search_units"] == 3
    assert seen[0]["search_counts"] == {"tavily/operator": {"calls": 2, "units": 3}}


async def test_search_usage_is_tenant_scoped(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    async with store.session() as db:
        other = await create_tenant(db, name="other")
    with pytest.raises(NotFoundError):
        await _search(store, other.id, session_id, turn_id)
    await _search(store, tenant_id, session_id, turn_id, calls=1, units=1)
    await _send_usage(store, settings, worker_id, session_id, turn_id)
    async with store.session() as db:
        assert await search_usage_for_turn(db, other.id, turn_id) == (0, 0, {})
        assert await get_turn_log(db, other.id, turn_id) is None
        empty = await usage_day(db, other.id, utc_now().date())
        assert empty["search_calls"] == 0
        assert empty["search_units"] == 0
        mine = await usage_day(db, tenant_id, utc_now().date())
        assert mine["search_calls"] == 1


async def test_search_usage_rejects_wrong_session(
    store: Store, settings: Settings
) -> None:
    worker_id = uuid.uuid4()
    tenant_id, _session_id, turn_id = await _leased(store, worker_id)
    async with store.session() as db:
        other_session = await create_session(
            db, tenant_id, environment={"type": "none"}, metadata={}
        )
    with pytest.raises(NotFoundError):
        await _search(store, tenant_id, other_session.id, turn_id)


async def test_search_usage_rejects_negative(store: Store, settings: Settings) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    with pytest.raises(ValueError):
        await _search(store, tenant_id, session_id, turn_id, calls=-1)


async def test_web_search_call_item_is_stored(store: Store) -> None:
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    async with store.session() as db:
        await create_item(
            db,
            tenant_id,
            session_id,
            type="web_search_call",
            turn_id=turn_id,
            data={
                "status": "completed",
                "action": {"type": "search", "query": "apipi"},
            },
        )
        items = await list_items(db, tenant_id, session_id)
    assert items is not None
    assert [item.type for item in items] == ["web_search_call"]


async def test_rollups_store_counts_search_before_usage(
    store: Store, settings: Settings
) -> None:
    rollups = settings.model_copy(update={"usage_store": "rollups"})
    worker_id = uuid.uuid4()
    tenant_id, session_id, turn_id = await _leased(store, worker_id)
    await _search(store, tenant_id, session_id, turn_id, calls=2, units=3)
    await _send_usage(store, rollups, worker_id, session_id, turn_id)
    async with store.session() as db:
        assert await get_turn_log(db, tenant_id, turn_id) is None
        day = await usage_day(db, tenant_id, utc_now().date())
    assert day["search_calls"] == 2
    assert day["search_units"] == 3
    assert day["turns"] == 1
