import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store.errors import NotFoundError
from apipi.store.models import (
    Agent,
    Artifact,
    EnvironmentRow,
    Event,
    FileRow,
    Item,
    SessionRow,
    SkillRow,
    Tenant,
    Turn,
    TurnLog,
    UploadRow,
    UsageRollup,
    Vault,
    VaultCredential,
    WorkerRow,
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
    model: str | None = None,
    instructions: str | None = None,
    status: str = "idle",
    environment: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    key_id: str = "",
    vault_ids: list[str] | None = None,
) -> SessionRow:
    row = SessionRow(
        tenant_id=tenant_id,
        agent_id=agent_id,
        model=model,
        instructions=instructions,
        status=status,
        environment=environment if environment is not None else {},
        metadata_json=metadata if metadata is not None else {},
        key_id=key_id,
        vault_ids=vault_ids if vault_ids is not None else [],
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


async def get_session_by_id(
    db: AsyncSession, session_id: uuid.UUID
) -> SessionRow | None:
    return await db.scalar(select(SessionRow).where(SessionRow.id == session_id))


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


async def create_environment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    environment_id: uuid.UUID,
    key_hash: str,
    status: str = "pending",
) -> EnvironmentRow:
    row = EnvironmentRow(
        id=environment_id,
        tenant_id=tenant_id,
        session_id=session_id,
        key_hash=key_hash,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def get_environment(
    db: AsyncSession, environment_id: uuid.UUID
) -> EnvironmentRow | None:
    return await db.scalar(
        select(EnvironmentRow).where(EnvironmentRow.id == environment_id)
    )


async def get_tenant_environment(
    db: AsyncSession, tenant_id: uuid.UUID, environment_id: uuid.UUID
) -> EnvironmentRow | None:
    return await db.scalar(
        select(EnvironmentRow).where(
            EnvironmentRow.tenant_id == tenant_id, EnvironmentRow.id == environment_id
        )
    )


async def get_session_environment(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> EnvironmentRow | None:
    return await db.scalar(
        select(EnvironmentRow).where(
            EnvironmentRow.tenant_id == tenant_id,
            EnvironmentRow.session_id == session_id,
        )
    )


async def update_environment(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    environment_id: uuid.UUID,
    *,
    status: str,
) -> EnvironmentRow | None:
    row = await get_tenant_environment(db, tenant_id, environment_id)
    if row is None:
        return None
    row.status = status
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


async def create_artifact(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    path: str,
    content_type: str = "application/octet-stream",
    turn_id: uuid.UUID | None = None,
    key_id: str = "",
    byte_size: int = 0,
) -> Artifact:
    artifact = Artifact(
        tenant_id=tenant_id,
        session_id=session_id,
        path=path,
        content_type=content_type,
        turn_id=turn_id,
        key_id=key_id,
        byte_size=byte_size,
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def get_artifact(
    db: AsyncSession, tenant_id: uuid.UUID, artifact_id: uuid.UUID
) -> Artifact | None:
    return await db.scalar(
        select(Artifact).where(
            Artifact.tenant_id == tenant_id, Artifact.id == artifact_id
        )
    )


async def get_session_artifact(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> Artifact | None:
    artifact = await get_artifact(db, tenant_id, artifact_id)
    if artifact is None or artifact.session_id != session_id:
        return None
    return artifact


async def list_artifacts(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[Artifact] | None:
    if await get_session(db, tenant_id, session_id) is None:
        return None
    result = await db.scalars(
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.session_id == session_id)
        .order_by(Artifact.created_at)
    )
    return list(result)


async def delete_session_artifact(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> Artifact | None:
    artifact = await get_session_artifact(db, tenant_id, session_id, artifact_id)
    if artifact is None:
        return None
    await db.delete(artifact)
    await db.flush()
    return artifact


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


async def append_turn_log(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    status: str,
    agent_id: uuid.UUID | None = None,
    model: str | None = None,
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    total_tokens: int = 0,
    error_code: str | None = None,
    request_id: str | None = None,
    tool_names: list[str] | None = None,
    tool_counts: dict[str, int] | None = None,
    mcp_names: list[str] | None = None,
    mcp_counts: dict[str, int] | None = None,
    key_id: str = "",
    environment_type: str = "",
    run_mode: str = "",
    instance_id: str | None = None,
    artifact_bytes: int = 0,
) -> TurnLog:
    row = TurnLog(
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        agent_id=agent_id,
        model=model,
        status=status,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=total_tokens,
        error_code=error_code,
        request_id=request_id,
        tool_names=list(tool_names) if tool_names is not None else [],
        tool_counts=dict(tool_counts) if tool_counts is not None else {},
        mcp_names=list(mcp_names) if mcp_names is not None else [],
        mcp_counts=dict(mcp_counts) if mcp_counts is not None else {},
        key_id=key_id,
        environment_type=environment_type,
        run_mode=run_mode,
        instance_id=instance_id,
        artifact_bytes=artifact_bytes,
    )
    db.add(row)
    await db.flush()
    return row


async def get_turn_log(
    db: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> TurnLog | None:
    return await db.scalar(
        select(TurnLog).where(
            TurnLog.tenant_id == tenant_id, TurnLog.turn_id == turn_id
        )
    )


async def list_turn_logs(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[TurnLog] | None:
    if await get_session(db, tenant_id, session_id) is None:
        return None
    result = await db.scalars(
        select(TurnLog)
        .where(TurnLog.tenant_id == tenant_id, TurnLog.session_id == session_id)
        .order_by(TurnLog.created_at)
    )
    return list(result)


async def usage_totals(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    session_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, int]:
    stmt = select(
        func.coalesce(func.sum(TurnLog.prompt_tokens), 0),
        func.coalesce(func.sum(TurnLog.completion_tokens), 0),
        func.coalesce(func.sum(TurnLog.cache_read_tokens), 0),
        func.coalesce(func.sum(TurnLog.cache_write_tokens), 0),
        func.coalesce(func.sum(TurnLog.total_tokens), 0),
        func.count(TurnLog.id),
    ).where(TurnLog.tenant_id == tenant_id)
    if session_id is not None:
        stmt = stmt.where(TurnLog.session_id == session_id)
    if turn_id is not None:
        stmt = stmt.where(TurnLog.turn_id == turn_id)
    if since is not None:
        stmt = stmt.where(TurnLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(TurnLog.created_at < until)
    row = (await db.execute(stmt)).one()
    return {
        "prompt_tokens": int(row[0]),
        "completion_tokens": int(row[1]),
        "cache_read_tokens": int(row[2]),
        "cache_write_tokens": int(row[3]),
        "total_tokens": int(row[4]),
        "turns": int(row[5]),
    }


async def add_usage_rollup(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    day: date,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    total_tokens: int = 0,
    turns: int = 1,
    artifact_bytes: int = 0,
) -> UsageRollup:
    row = await db.scalar(
        select(UsageRollup).where(
            UsageRollup.tenant_id == tenant_id, UsageRollup.day == day
        )
    )
    if row is None:
        row = UsageRollup(
            tenant_id=tenant_id,
            day=day,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            total_tokens=total_tokens,
            turns=turns,
            artifact_bytes=artifact_bytes,
        )
        db.add(row)
        await db.flush()
        return row
    row.prompt_tokens += prompt_tokens
    row.completion_tokens += completion_tokens
    row.cache_read_tokens += cache_read_tokens
    row.cache_write_tokens += cache_write_tokens
    row.total_tokens += total_tokens
    row.turns += turns
    row.artifact_bytes += artifact_bytes
    await db.flush()
    return row


async def usage_day(
    db: AsyncSession, tenant_id: uuid.UUID, day: date
) -> dict[str, int]:
    row = await db.scalar(
        select(UsageRollup).where(
            UsageRollup.tenant_id == tenant_id, UsageRollup.day == day
        )
    )
    if row is not None:
        return {
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "total_tokens": row.total_tokens,
            "turns": row.turns,
        }
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return await usage_totals(
        db, tenant_id, since=start, until=start + timedelta(days=1)
    )


async def artifact_bytes_for_turn(
    db: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> int:
    value = await db.scalar(
        select(func.coalesce(func.sum(Artifact.byte_size), 0)).where(
            Artifact.tenant_id == tenant_id, Artifact.turn_id == turn_id
        )
    )
    return int(value or 0)


async def purge_turn_logs(db: AsyncSession, older_than: datetime) -> int:
    result = await db.execute(delete(TurnLog).where(TurnLog.created_at < older_than))
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def upsert_worker(
    db: AsyncSession,
    worker_id: uuid.UUID,
    *,
    capacity: int,
    memory_mb: int,
    api_instance_id: str | None = None,
) -> WorkerRow:
    row = await db.scalar(select(WorkerRow).where(WorkerRow.id == worker_id))
    if row is None:
        row = WorkerRow(
            id=worker_id,
            capacity=capacity,
            memory_mb=memory_mb,
            generation=1,
            last_seen=utc_now(),
            api_instance_id=api_instance_id,
        )
        db.add(row)
    else:
        row.capacity = capacity
        row.memory_mb = memory_mb
        row.generation += 1
        row.last_seen = utc_now()
        row.api_instance_id = api_instance_id
    await db.flush()
    return row


async def get_worker(db: AsyncSession, worker_id: uuid.UUID) -> WorkerRow | None:
    return await db.scalar(select(WorkerRow).where(WorkerRow.id == worker_id))


async def touch_worker(
    db: AsyncSession,
    worker_id: uuid.UUID,
    *,
    capacity: int | None = None,
    memory_mb: int | None = None,
    api_instance_id: str | None = None,
) -> WorkerRow | None:
    row = await get_worker(db, worker_id)
    if row is None:
        return None
    row.last_seen = utc_now()
    if capacity is not None:
        row.capacity = capacity
    if memory_mb is not None:
        row.memory_mb = memory_mb
    if api_instance_id is not None:
        row.api_instance_id = api_instance_id
    await db.flush()
    return row


async def clear_worker_api_instance(
    db: AsyncSession, worker_id: uuid.UUID, *, instance_id: str | None
) -> None:
    row = await get_worker(db, worker_id)
    if row is None:
        return
    if row.api_instance_id == instance_id:
        row.api_instance_id = None
        await db.flush()


async def set_session_lease(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    worker_id: uuid.UUID,
    lease_id: uuid.UUID,
    lease_until: datetime,
) -> SessionRow | None:
    now = utc_now()
    result = await db.execute(
        update(SessionRow)
        .where(
            SessionRow.tenant_id == tenant_id,
            SessionRow.id == session_id,
            or_(SessionRow.lease_id.is_(None), SessionRow.lease_until < func.now()),
        )
        .values(
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=lease_until,
            updated_at=now,
        )
        .returning(SessionRow.id)
        .execution_options(synchronize_session=False)
    )
    updated = result.scalar_one_or_none()
    if updated is None:
        return None
    row = await get_session(db, tenant_id, session_id)
    if row is not None:
        await db.refresh(row)
    return row


async def clear_session_lease(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        return None
    row.worker_id = None
    row.lease_id = None
    row.lease_until = None
    row.updated_at = utc_now()
    await db.flush()
    return row


async def get_session_by_lease(
    db: AsyncSession, lease_id: uuid.UUID
) -> SessionRow | None:
    return await db.scalar(select(SessionRow).where(SessionRow.lease_id == lease_id))


async def list_expired_leases(db: AsyncSession, now: datetime) -> list[SessionRow]:
    result = await db.scalars(
        select(SessionRow)
        .where(SessionRow.lease_id.is_not(None), SessionRow.lease_until <= now)
        .with_for_update(skip_locked=True)
    )
    return list(result)


async def list_worker_leases(
    db: AsyncSession, worker_id: uuid.UUID
) -> list[SessionRow]:
    result = await db.scalars(
        select(SessionRow).where(SessionRow.worker_id == worker_id)
    )
    return list(result)


async def extend_worker_leases(
    db: AsyncSession, worker_id: uuid.UUID, *, lease_until: datetime
) -> None:
    await db.execute(
        update(SessionRow)
        .where(
            SessionRow.worker_id == worker_id,
            SessionRow.lease_id.is_not(None),
        )
        .values(lease_until=lease_until, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )


async def create_vault(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Vault:
    row = Vault(
        tenant_id=tenant_id,
        name=name,
        metadata_json=metadata if metadata is not None else {},
    )
    db.add(row)
    await db.flush()
    return row


async def list_vaults(db: AsyncSession, tenant_id: uuid.UUID) -> list[Vault]:
    result = await db.scalars(select(Vault).where(Vault.tenant_id == tenant_id))
    return list(result)


async def get_vault(
    db: AsyncSession, tenant_id: uuid.UUID, vault_id: uuid.UUID
) -> Vault | None:
    return await db.scalar(
        select(Vault).where(Vault.tenant_id == tenant_id, Vault.id == vault_id)
    )


async def update_vault(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vault_id: uuid.UUID,
    *,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Vault | None:
    row = await get_vault(db, tenant_id, vault_id)
    if row is None:
        return None
    if name is not None:
        row.name = name
    if metadata is not None:
        row.metadata_json = metadata
    row.updated_at = utc_now()
    await db.flush()
    return row


async def delete_vault(
    db: AsyncSession, tenant_id: uuid.UUID, vault_id: uuid.UUID
) -> bool:
    row = await get_vault(db, tenant_id, vault_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def create_vault_credential(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vault_id: uuid.UUID,
    *,
    name: str | None = None,
    auth_type: str,
    mcp_server_url: str,
    token: str,
) -> VaultCredential:
    row = VaultCredential(
        tenant_id=tenant_id,
        vault_id=vault_id,
        name=name,
        auth_type=auth_type,
        mcp_server_url=mcp_server_url,
        token=token,
    )
    db.add(row)
    await db.flush()
    return row


async def list_vault_credentials(
    db: AsyncSession, tenant_id: uuid.UUID, vault_id: uuid.UUID
) -> list[VaultCredential]:
    result = await db.scalars(
        select(VaultCredential).where(
            VaultCredential.tenant_id == tenant_id,
            VaultCredential.vault_id == vault_id,
        )
    )
    return list(result)


async def get_vault_credential(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
) -> VaultCredential | None:
    return await db.scalar(
        select(VaultCredential).where(
            VaultCredential.tenant_id == tenant_id,
            VaultCredential.vault_id == vault_id,
            VaultCredential.id == credential_id,
        )
    )


async def update_vault_credential(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    *,
    name: str | None = None,
    token: str | None = None,
) -> VaultCredential | None:
    row = await get_vault_credential(db, tenant_id, vault_id, credential_id)
    if row is None:
        return None
    if name is not None:
        row.name = name
    if token is not None:
        row.token = token
    row.updated_at = utc_now()
    await db.flush()
    return row


async def delete_vault_credential(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
) -> bool:
    row = await get_vault_credential(db, tenant_id, vault_id, credential_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def list_credentials_for_vault_ids(
    db: AsyncSession, tenant_id: uuid.UUID, vault_ids: list[uuid.UUID]
) -> list[VaultCredential]:
    if not vault_ids:
        return []
    result = await db.scalars(
        select(VaultCredential).where(
            VaultCredential.tenant_id == tenant_id,
            VaultCredential.vault_id.in_(vault_ids),
        )
    )
    return list(result)


async def create_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    file_id: str,
    filename: str,
    purpose: str,
    size: int,
    content_type: str | None = None,
) -> FileRow:
    row = FileRow(
        id=file_id,
        tenant_id=tenant_id,
        filename=filename,
        purpose=purpose,
        size=size,
        content_type=content_type,
    )
    db.add(row)
    await db.flush()
    return row


async def get_file(
    db: AsyncSession, tenant_id: uuid.UUID, file_id: str
) -> FileRow | None:
    return await db.scalar(
        select(FileRow).where(FileRow.tenant_id == tenant_id, FileRow.id == file_id)
    )


async def list_files(db: AsyncSession, tenant_id: uuid.UUID) -> list[FileRow]:
    result = await db.scalars(
        select(FileRow)
        .where(FileRow.tenant_id == tenant_id)
        .order_by(FileRow.created_at.desc())
    )
    return list(result)


async def delete_file(db: AsyncSession, tenant_id: uuid.UUID, file_id: str) -> bool:
    row = await get_file(db, tenant_id, file_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def create_skill(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    skill_id: str,
    name: str,
    size: int,
) -> SkillRow:
    row = SkillRow(id=skill_id, tenant_id=tenant_id, name=name, size=size)
    db.add(row)
    await db.flush()
    return row


async def get_skill(
    db: AsyncSession, tenant_id: uuid.UUID, skill_id: str
) -> SkillRow | None:
    return await db.scalar(
        select(SkillRow).where(SkillRow.tenant_id == tenant_id, SkillRow.id == skill_id)
    )


async def list_skills(db: AsyncSession, tenant_id: uuid.UUID) -> list[SkillRow]:
    result = await db.scalars(
        select(SkillRow)
        .where(SkillRow.tenant_id == tenant_id)
        .order_by(SkillRow.created_at.desc())
    )
    return list(result)


async def delete_skill(db: AsyncSession, tenant_id: uuid.UUID, skill_id: str) -> bool:
    row = await get_skill(db, tenant_id, skill_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def create_upload(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    purpose: str,
    object_id: str,
    filename: str,
    content_type: str,
    declared_bytes: int,
    expires_at: datetime,
) -> UploadRow:
    row = UploadRow(
        tenant_id=tenant_id,
        purpose=purpose,
        object_id=object_id,
        filename=filename,
        content_type=content_type,
        declared_bytes=declared_bytes,
        status="pending",
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def get_upload(
    db: AsyncSession, tenant_id: uuid.UUID, upload_id: uuid.UUID
) -> UploadRow | None:
    return await db.scalar(
        select(UploadRow).where(
            UploadRow.tenant_id == tenant_id, UploadRow.id == upload_id
        )
    )
