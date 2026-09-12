import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store.errors import NotFoundError
from apipi.store.events import append_event, list_events
from apipi.store.repo import (
    create_agent,
    create_api_key,
    create_item,
    create_session,
    create_tenant,
    create_turn,
    get_agent,
    get_api_key_by_hash,
    get_item,
    get_session,
    get_turn,
    list_agents,
)


async def test_event_seq_and_replay(db: AsyncSession) -> None:
    tenant = await create_tenant(db, name="a")
    session_row = await create_session(db, tenant.id)
    first = await append_event(
        db, tenant.id, session_row.id, type="agent.session.created"
    )
    second = await append_event(
        db,
        tenant.id,
        session_row.id,
        type="agent.session.idle",
        data={"status": "idle"},
    )
    assert first.seq == 1
    assert second.seq == 2
    all_events = await list_events(db, tenant.id, session_row.id)
    assert [event.type for event in all_events] == [
        "agent.session.created",
        "agent.session.idle",
    ]
    replay = await list_events(db, tenant.id, session_row.id, after_seq=1)
    assert [event.seq for event in replay] == [2]


async def test_append_event_unknown_session(db: AsyncSession) -> None:
    tenant = await create_tenant(db, name="a")
    with pytest.raises(NotFoundError):
        await append_event(db, tenant.id, uuid.uuid4(), type="agent.session.created")


async def test_events_are_tenant_scoped(db: AsyncSession) -> None:
    a = await create_tenant(db, name="a")
    b = await create_tenant(db, name="b")
    session_a = await create_session(db, a.id)
    await append_event(db, a.id, session_a.id, type="agent.session.created")
    assert await list_events(db, b.id, session_a.id) == []
    with pytest.raises(NotFoundError):
        await append_event(db, b.id, session_a.id, type="agent.session.idle")


async def test_agents_are_tenant_scoped(db: AsyncSession) -> None:
    a = await create_tenant(db, name="a")
    b = await create_tenant(db, name="b")
    agent = await create_agent(db, a.id, name="one", model="test")
    assert await get_agent(db, a.id, agent.id) is not None
    assert await get_agent(db, b.id, agent.id) is None
    assert await list_agents(db, b.id) == []


async def test_sessions_turns_items_are_tenant_scoped(db: AsyncSession) -> None:
    a = await create_tenant(db, name="a")
    b = await create_tenant(db, name="b")
    session_row = await create_session(db, a.id)
    turn = await create_turn(db, a.id, session_row.id, status="completed")
    item = await create_item(
        db, a.id, session_row.id, type="message", turn_id=turn.id, data={"role": "user"}
    )
    assert await get_session(db, b.id, session_row.id) is None
    assert await get_turn(db, b.id, turn.id) is None
    assert await get_item(db, b.id, item.id) is None


async def test_api_key_hash_roundtrip(db: AsyncSession) -> None:
    tenant = await create_tenant(db, name="a")
    key = await create_api_key(db, tenant.id, token_hash="a" * 64)
    found = await get_api_key_by_hash(db, "a" * 64)
    assert found is not None
    assert found.id == key.id
    assert found.tenant_id == tenant.id
    assert await get_api_key_by_hash(db, "b" * 64) is None


async def test_event_module_is_append_only() -> None:
    from apipi.store import events

    assert set(events.__all__) == {"append_event", "list_events"}
    assert not hasattr(events, "update_event")
    assert not hasattr(events, "delete_event")
    assert not hasattr(events, "rewrite")
