import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store.errors import NotFoundError
from apipi.store.models import (
    Agent,
    Event,
    Item,
    SessionRow,
    Tenant,
    Turn,
    utc_now,
)


async def create_tenant(db: AsyncSession, *, name: str) -> Tenant:
    tenant = Tenant(name=name)
    db.add(tenant)
    await db.flush()
    return tenant


async def get_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    return await db.scalar(select(Tenant).where(Tenant.id == tenant_id))


async def ensure_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await get_tenant(db, tenant_id)
    if tenant is not None:
        return tenant
    tenant = Tenant(id=tenant_id, name=str(tenant_id))
    db.add(tenant)
    await db.flush()
    return tenant


async def create_agent(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str | None = None,
    model: str | None = None,
    instructions: str | None = None,
    metadata: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
) -> Agent:
    agent = Agent(
        tenant_id=tenant_id,
        name=name,
        model=model,
        instructions=instructions,
        metadata_json=metadata if metadata is not None else {},
        tools=tools if tools is not None else [],
    )
    db.add(agent)
    await db.flush()
    return agent


async def get_agent(
    db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> Agent | None:
    return await db.scalar(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.id == agent_id)
    )


async def list_agents(db: AsyncSession, tenant_id: uuid.UUID) -> list[Agent]:
    result = await db.scalars(
        select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at)
    )
    return list(result)


async def update_agent(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    changes: dict[str, Any],
) -> Agent | None:
    agent = await get_agent(db, tenant_id, agent_id)
    if agent is None:
        return None
    if "name" in changes:
        agent.name = changes["name"]
    if "model" in changes:
        agent.model = changes["model"]
    if "instructions" in changes:
        agent.instructions = changes["instructions"]
    if "metadata" in changes:
        agent.metadata_json = changes["metadata"]
    if "tools" in changes:
        agent.tools = changes["tools"]
    agent.updated_at = utc_now()
    await db.flush()
    return agent


async def delete_agent(
    db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    agent = await get_agent(db, tenant_id, agent_id)
    if agent is None:
        return False
    await db.delete(agent)
    await db.flush()
    return True


async def create_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    status: str = "idle",
    environment: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SessionRow:
    row = SessionRow(
        tenant_id=tenant_id,
        agent_id=agent_id,
        status=status,
        environment=environment if environment is not None else {},
        metadata_json=metadata if metadata is not None else {},
    )
    db.add(row)
    await db.flush()
    return row


async def get_session(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> SessionRow | None:
    return await db.scalar(
        select(SessionRow).where(
            SessionRow.tenant_id == tenant_id, SessionRow.id == session_id
        )
    )


async def list_sessions(db: AsyncSession, tenant_id: uuid.UUID) -> list[SessionRow]:
    result = await db.scalars(
        select(SessionRow)
        .where(SessionRow.tenant_id == tenant_id)
        .order_by(SessionRow.created_at)
    )
    return list(result)


async def update_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    changes: dict[str, Any],
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        return None
    if "status" in changes:
        row.status = changes["status"]
    if "metadata" in changes:
        row.metadata_json = changes["metadata"]
    if "environment" in changes:
        row.environment = changes["environment"]
    if "required_actions" in changes:
        row.required_actions = changes["required_actions"]
    row.updated_at = utc_now()
    await db.flush()
    return row


async def delete_session(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def create_turn(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    status: str,
    usage: dict[str, Any] | None = None,
) -> Turn:
    turn = Turn(tenant_id=tenant_id, session_id=session_id, status=status, usage=usage)
    db.add(turn)
    await db.flush()
    return turn


async def get_turn(
    db: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> Turn | None:
    return await db.scalar(
        select(Turn).where(Turn.tenant_id == tenant_id, Turn.id == turn_id)
    )


async def create_item(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
    turn_id: uuid.UUID | None = None,
) -> Item:
    item = Item(
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        type=type,
        data=data if data is not None else {},
    )
    db.add(item)
    await db.flush()
    return item


async def get_item(
    db: AsyncSession, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Item | None:
    return await db.scalar(
        select(Item).where(Item.tenant_id == tenant_id, Item.id == item_id)
    )


async def list_turns(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[Turn] | None:
    if await get_session(db, tenant_id, session_id) is None:
        return None
    result = await db.scalars(
        select(Turn)
        .where(Turn.tenant_id == tenant_id, Turn.session_id == session_id)
        .order_by(Turn.created_at)
    )
    return list(result)


async def get_session_turn(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> Turn | None:
    turn = await get_turn(db, tenant_id, turn_id)
    if turn is None or turn.session_id != session_id:
        return None
    return turn


async def list_items(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[Item] | None:
    if await get_session(db, tenant_id, session_id) is None:
        return None
    result = await db.scalars(
        select(Item)
        .where(Item.tenant_id == tenant_id, Item.session_id == session_id)
        .order_by(Item.created_at)
    )
    return list(result)


async def append_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
) -> Event:
    session_row = await db.scalar(
        select(SessionRow)
        .where(SessionRow.tenant_id == tenant_id, SessionRow.id == session_id)
        .with_for_update()
    )
    if session_row is None:
        raise NotFoundError
    max_seq = await db.scalar(
        select(func.coalesce(func.max(Event.seq), 0)).where(
            Event.tenant_id == tenant_id, Event.session_id == session_id
        )
    )
    if max_seq is None:
        max_seq = 0
    event = Event(
        tenant_id=tenant_id,
        session_id=session_id,
        seq=max_seq + 1,
        type=type,
        data=data if data is not None else {},
    )
    db.add(event)
    await db.flush()
    return event


async def list_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    after_seq: int | None = None,
) -> list[Event]:
    stmt = select(Event).where(
        Event.tenant_id == tenant_id, Event.session_id == session_id
    )
    if after_seq is not None:
        stmt = stmt.where(Event.seq > after_seq)
    stmt = stmt.order_by(Event.seq)
    result = await db.scalars(stmt)
    return list(result)
