import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store import turn_logs
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
)
from apipi.store.turn_logs import (
    append_turn_log,
    get_turn_log,
    list_turn_logs,
    usage_totals,
)


def test_turn_log_module_is_append_only() -> None:
    public = {
        name
        for name, value in inspect.getmembers(turn_logs)
        if inspect.iscoroutinefunction(value)
    }
    assert public == {
        "append_turn_log",
        "get_turn_log",
        "list_turn_logs",
        "usage_totals",
    }
    assert not hasattr(turn_logs, "update_turn_log")
    assert not hasattr(turn_logs, "delete_turn_log")
    assert not hasattr(turn_logs, "rewrite")


async def test_turn_logs_are_tenant_scoped(db: AsyncSession) -> None:
    a = await create_tenant(db, name="a")
    b = await create_tenant(db, name="b")
    session_row = await create_session(db, a.id)
    turn = await create_turn(db, a.id, session_row.id, status="completed")
    row = await append_turn_log(
        db,
        a.id,
        session_row.id,
        turn.id,
        status="completed",
        prompt_tokens=4,
        tool_names=["echo"],
        tool_counts={"echo": 1},
    )
    blob = json.dumps(
        {column.key: getattr(row, column.key) for column in row.__table__.columns},
        default=str,
    )
    assert "secret-prompt" not in blob
    assert await get_turn_log(db, a.id, turn.id) is not None
    assert await get_turn_log(db, b.id, turn.id) is None
    assert await list_turn_logs(db, b.id, session_row.id) is None
    listed = await list_turn_logs(db, a.id, session_row.id)
    assert listed is not None
    assert len(listed) == 1
    assert listed[0].id == row.id
    assert listed[0].prompt_tokens == 4
    assert listed[0].tool_names == ["echo"]
    assert await usage_totals(db, a.id, session_id=session_row.id) == {
        "prompt_tokens": 4,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "turns": 1,
    }
    assert await usage_totals(db, b.id, session_id=session_row.id) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "turns": 0,
    }


async def test_append_turn_log_has_no_message_fields(db: AsyncSession) -> None:
    tenant = await create_tenant(db, name="a")
    session_row = await create_session(db, tenant.id)
    turn = await create_turn(db, tenant.id, session_row.id, status="failed")
    row = await append_turn_log(
        db,
        tenant.id,
        session_row.id,
        turn.id,
        status="failed",
        error_code="invalid_request",
        mcp_names=["tavily"],
        mcp_counts={"tavily": 2},
    )
    assert row.request_id is None
    assert row.error_code == "invalid_request"
    assert row.mcp_names == ["tavily"]
    assert row.mcp_counts == {"tavily": 2}
    assert not hasattr(row, "prompt")
    assert not hasattr(row, "completion")
    assert "content" not in {column.key for column in row.__table__.columns}
    assert uuid.UUID(str(row.turn_id)) == turn.id


async def test_usage_totals_by_day(db: AsyncSession) -> None:
    tenant = await create_tenant(db, name="a")
    session_row = await create_session(db, tenant.id)
    first = await create_turn(db, tenant.id, session_row.id, status="completed")
    second = await create_turn(db, tenant.id, session_row.id, status="completed")
    old = await append_turn_log(
        db,
        tenant.id,
        session_row.id,
        first.id,
        status="completed",
        prompt_tokens=4,
        total_tokens=4,
    )
    old.created_at = datetime(2020, 1, 2, 12, 0, tzinfo=UTC)
    await append_turn_log(
        db,
        tenant.id,
        session_row.id,
        second.id,
        status="completed",
        prompt_tokens=3,
        total_tokens=3,
    )
    await db.flush()
    start = datetime(2020, 1, 2, tzinfo=UTC)
    assert await usage_totals(
        db, tenant.id, since=start, until=start + timedelta(days=1)
    ) == {
        "prompt_tokens": 4,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 4,
        "turns": 1,
    }
    assert await usage_totals(db, tenant.id, session_id=session_row.id) == {
        "prompt_tokens": 7,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 7,
        "turns": 2,
    }
