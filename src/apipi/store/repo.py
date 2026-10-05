import uuid
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apipi.store.errors import NotFoundError
from apipi.store.models import (
    Agent,
    Artifact,
    ArtifactUploadRow,
    EnvironmentRow,
    Event,
    FileRow,
    Item,
    SearchTurnCount,
    SessionFileRow,
    SessionRow,
    SkillRow,
    TemplateRow,
    Tenant,
    Turn,
    TurnLog,
    UploadRow,
    UsageRollup,
    Vault,
    VaultCredential,
    WorkerForward,
    WorkerRow,
    WorkerToken,
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
    idle_ttl: str | None = None,
    metadata: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
    session_defaults: dict[str, Any] | None = None,
) -> Agent:
    agent = Agent(
        tenant_id=tenant_id,
        name=name,
        model=model,
        instructions=instructions,
        idle_ttl=idle_ttl,
        metadata_json=metadata if metadata is not None else {},
        tools=tools if tools is not None else [],
        session_defaults=session_defaults,
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
    if "idle_ttl" in changes:
        agent.idle_ttl = changes["idle_ttl"]
    if "metadata" in changes:
        agent.metadata_json = changes["metadata"]
    if "tools" in changes:
        agent.tools = changes["tools"]
    if "session_defaults" in changes:
        agent.session_defaults = changes["session_defaults"]
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
    idle_ttl: str | None = None,
    status: str = "idle",
    environment: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    key_id: str = "",
    user_id: str | None = None,
    org_id: str | None = None,
    vault_ids: list[str] | None = None,
    tools: list[Any] | None = None,
) -> SessionRow:
    row = SessionRow(
        tenant_id=tenant_id,
        agent_id=agent_id,
        model=model,
        instructions=instructions,
        idle_ttl=idle_ttl,
        status=status,
        environment=environment if environment is not None else {},
        metadata_json=metadata if metadata is not None else {},
        key_id=key_id,
        user_id=user_id,
        org_id=org_id,
        vault_ids=vault_ids if vault_ids is not None else [],
        tools=tools,
    )
    db.add(row)
    await db.flush()
    return row


def _user_clause(user_id: str | None) -> list[Any]:
    if user_id is None:
        return []
    return [SessionRow.user_id == user_id]


async def get_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    user_id: str | None = None,
) -> SessionRow | None:
    return await db.scalar(
        select(SessionRow).where(
            SessionRow.tenant_id == tenant_id,
            SessionRow.id == session_id,
            *_user_clause(user_id),
        )
    )


async def get_session_by_id(
    db: AsyncSession, session_id: uuid.UUID
) -> SessionRow | None:
    return await db.scalar(select(SessionRow).where(SessionRow.id == session_id))


async def get_sessions_by_ids(
    db: AsyncSession, session_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, SessionRow]:
    if not session_ids:
        return {}
    rows = await db.scalars(select(SessionRow).where(SessionRow.id.in_(session_ids)))
    return {row.id: row for row in rows}


async def agent_idle_ttls(
    db: AsyncSession, agent_ids: Collection[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], str | None]:
    """The idle TTL of each agent, keyed by (tenant_id, agent_id), in one query."""
    if not agent_ids:
        return {}
    result = await db.execute(
        select(Agent.tenant_id, Agent.id, Agent.idle_ttl).where(Agent.id.in_(agent_ids))
    )
    return {(tenant_id, agent_id): ttl for tenant_id, agent_id, ttl in result}


async def list_sessions(
    db: AsyncSession, tenant_id: uuid.UUID, *, user_id: str | None = None
) -> list[SessionRow]:
    result = await db.scalars(
        select(SessionRow)
        .where(SessionRow.tenant_id == tenant_id, *_user_clause(user_id))
        .order_by(SessionRow.created_at)
    )
    return list(result)


async def update_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    changes: dict[str, Any],
    user_id: str | None = None,
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    user_id: str | None = None,
) -> bool:
    row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
    turn_id: uuid.UUID | None = None,
) -> Turn:
    # Turns are ordered by `created_at` (newest turn, turn list). The wall
    # clock can step back between two turns, or differ between API
    # replicas, so a new turn never gets a stamp at or before the turn
    # created before it. Creation is serialized by the session row lock.
    created_at = utc_now()
    newest = await db.scalar(
        select(func.max(Turn.created_at)).where(
            Turn.tenant_id == tenant_id, Turn.session_id == session_id
        )
    )
    if newest is not None:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=UTC)
        if created_at <= newest:
            created_at = newest + timedelta(microseconds=1)
    turn = Turn(
        id=turn_id if turn_id is not None else uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        status=status,
        usage=usage,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(turn)
    await db.flush()
    return turn


async def get_turn(
    db: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> Turn | None:
    return await db.scalar(
        select(Turn).where(Turn.tenant_id == tenant_id, Turn.id == turn_id)
    )


async def get_running_turn(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Turn | None:
    """The session's in-progress turn, if any (newest first)."""
    return await db.scalar(
        select(Turn)
        .where(
            Turn.tenant_id == tenant_id,
            Turn.session_id == session_id,
            Turn.status == "in_progress",
        )
        .order_by(Turn.created_at.desc())
        .limit(1)
    )


async def get_latest_turn(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Turn | None:
    """The session's newest turn, running or already finished."""
    return await db.scalar(
        select(Turn)
        .where(Turn.tenant_id == tenant_id, Turn.session_id == session_id)
        .order_by(Turn.created_at.desc())
        .limit(1)
    )


async def get_item(
    db: AsyncSession, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> Item | None:
    return await db.scalar(
        select(Item).where(Item.tenant_id == tenant_id, Item.id == item_id)
    )


async def create_item(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
    turn_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
) -> Item:
    item = Item(
        id=item_id if item_id is not None else uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        type=type,
        data=data if data is not None else {},
    )
    db.add(item)
    await db.flush()
    return item


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
    artifact_id: uuid.UUID | None = None,
) -> Artifact:
    artifact = Artifact(
        id=artifact_id if artifact_id is not None else uuid.uuid4(),
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
    failure_source: str | None = None,
    upstream_status: int | None = None,
    retryable: bool | None = None,
    legacy_code: str | None = None,
    upstream_attempts: int | None = None,
    request_id: str | None = None,
    tool_names: list[str] | None = None,
    tool_counts: dict[str, int] | None = None,
    mcp_names: list[str] | None = None,
    mcp_counts: dict[str, int] | None = None,
    search_calls: int = 0,
    search_units: int = 0,
    search_counts: dict[str, dict[str, int]] | None = None,
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
        failure_source=failure_source,
        upstream_status=upstream_status,
        retryable=retryable,
        legacy_code=legacy_code,
        upstream_attempts=upstream_attempts,
        request_id=request_id,
        tool_names=list(tool_names) if tool_names is not None else [],
        tool_counts=dict(tool_counts) if tool_counts is not None else {},
        mcp_names=list(mcp_names) if mcp_names is not None else [],
        mcp_counts=dict(mcp_counts) if mcp_counts is not None else {},
        search_calls=search_calls,
        search_units=search_units,
        search_counts=(
            {key: dict(value) for key, value in search_counts.items()}
            if search_counts is not None
            else {}
        ),
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
        func.coalesce(func.sum(TurnLog.search_calls), 0),
        func.coalesce(func.sum(TurnLog.search_units), 0),
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
        "search_calls": int(row[6]),
        "search_units": int(row[7]),
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
    search_calls: int = 0,
    search_units: int = 0,
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
            search_calls=search_calls,
            search_units=search_units,
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
    row.search_calls += search_calls
    row.search_units += search_units
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
            "search_calls": row.search_calls,
            "search_units": row.search_units,
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


async def lock_turn(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID
) -> bool:
    found = await db.scalar(
        select(Turn.id)
        .where(
            Turn.tenant_id == tenant_id,
            Turn.session_id == session_id,
            Turn.id == turn_id,
        )
        .with_for_update()
    )
    return found is not None


async def search_usage_for_turn(
    db: AsyncSession, tenant_id: uuid.UUID, turn_id: uuid.UUID
) -> tuple[int, int, dict[str, dict[str, int]]]:
    rows = await db.scalars(
        select(SearchTurnCount)
        .where(
            SearchTurnCount.tenant_id == tenant_id, SearchTurnCount.turn_id == turn_id
        )
        .order_by(SearchTurnCount.provider, SearchTurnCount.key_source)
    )
    calls = 0
    units = 0
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        calls += row.calls
        units += row.units
        counts[f"{row.provider}/{row.key_source}"] = {
            "calls": row.calls,
            "units": row.units,
        }
    return calls, units, counts


async def record_search_usage(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    provider: str,
    key_source: str,
    calls: int,
    units: int,
) -> None:
    if calls < 0 or units < 0:
        raise ValueError("search usage cannot be negative")
    if not await lock_turn(db, tenant_id, session_id, turn_id):
        raise NotFoundError("turn not found")
    row = await db.scalar(
        select(SearchTurnCount).where(
            SearchTurnCount.tenant_id == tenant_id,
            SearchTurnCount.turn_id == turn_id,
            SearchTurnCount.provider == provider,
            SearchTurnCount.key_source == key_source,
        )
    )
    if row is None:
        db.add(
            SearchTurnCount(
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
                provider=provider,
                key_source=key_source,
                calls=calls,
                units=units,
            )
        )
    else:
        row.calls += calls
        row.units += units
    await db.flush()
    log_row = await get_turn_log(db, tenant_id, turn_id)
    if log_row is None:
        return
    key = f"{provider}/{key_source}"
    counts = {name: dict(value) for name, value in log_row.search_counts.items()}
    current = counts.get(key, {"calls": 0, "units": 0})
    counts[key] = {
        "calls": int(current.get("calls", 0)) + calls,
        "units": int(current.get("units", 0)) + units,
    }
    log_row.search_counts = counts
    log_row.search_calls += calls
    log_row.search_units += units
    await db.flush()
    logged = log_row.created_at
    if logged.tzinfo is not None:
        logged = logged.astimezone(UTC)
    await add_usage_rollup(
        db,
        tenant_id,
        logged.date(),
        turns=0,
        search_calls=calls,
        search_units=units,
    )


async def purge_turn_logs(db: AsyncSession, older_than: datetime) -> int:
    await db.execute(
        delete(SearchTurnCount).where(SearchTurnCount.created_at < older_than)
    )
    result = await db.execute(delete(TurnLog).where(TurnLog.created_at < older_than))
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


def _apply_fleet(row: WorkerRow, fleet: dict[str, Any] | None) -> None:
    if not fleet:
        return
    for key in ("accepts", "images", "arch", "draining"):
        if key in fleet:
            setattr(row, key, fleet[key])


async def upsert_worker(
    db: AsyncSession,
    worker_id: uuid.UUID,
    *,
    capacity: int,
    memory_mb: int,
    api_instance_id: str | None = None,
    fleet: dict[str, Any] | None = None,
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
    _apply_fleet(row, fleet)
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
    generation: int | None = None,
    fleet: dict[str, Any] | None = None,
) -> WorkerRow | None:
    """Record a sign of life. A superseded `generation` leaves the row as it is."""
    row = await get_worker(db, worker_id)
    if row is None:
        return None
    if generation is not None and row.generation != generation:
        return row
    row.last_seen = utc_now()
    if capacity is not None:
        row.capacity = capacity
    if memory_mb is not None:
        row.memory_mb = memory_mb
    if api_instance_id is not None:
        row.api_instance_id = api_instance_id
    _apply_fleet(row, fleet)
    await db.flush()
    return row


async def list_live_workers(
    db: AsyncSession, *, seen_after: datetime, exclude_instance: str | None = None
) -> list[WorkerRow]:
    """Workers holding a socket on some replica and heard from since `seen_after`."""
    query = select(WorkerRow).where(
        WorkerRow.api_instance_id.is_not(None), WorkerRow.last_seen >= seen_after
    )
    if exclude_instance is not None:
        query = query.where(WorkerRow.api_instance_id != exclude_instance)
    return list(await db.scalars(query))


async def list_leased_environments(
    db: AsyncSession, worker_ids: Collection[uuid.UUID]
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    """The worker and environment of every session leased to one of `worker_ids`."""
    if not worker_ids:
        return []
    result = await db.execute(
        select(SessionRow.worker_id, SessionRow.environment).where(
            SessionRow.worker_id.in_(list(worker_ids)),
            SessionRow.lease_id.is_not(None),
        )
    )
    return [(row[0], row[1] if isinstance(row[1], dict) else {}) for row in result]


async def create_worker_forward(db: AsyncSession, row: WorkerForward) -> None:
    db.add(row)
    await db.flush()


async def get_worker_forward(
    db: AsyncSession, forward_id: uuid.UUID
) -> WorkerForward | None:
    row = await db.scalar(select(WorkerForward).where(WorkerForward.id == forward_id))
    if row is not None:
        await db.refresh(row)
    return row


async def claim_worker_forward(
    db: AsyncSession, forward_id: uuid.UUID, *, target: str
) -> WorkerForward | None:
    """Claim a pending forward addressed to `target`; None if it was taken."""
    result = await db.execute(
        update(WorkerForward)
        .where(
            WorkerForward.id == forward_id,
            WorkerForward.target == target,
            WorkerForward.status == "pending",
        )
        .values(status="claimed", updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if not getattr(result, "rowcount", 0):
        return None
    return await get_worker_forward(db, forward_id)


async def set_worker_forward_status(
    db: AsyncSession,
    forward_id: uuid.UUID,
    status: str,
    *,
    code: str | None = None,
    message: str | None = None,
    http_status: int | None = None,
    only_from: str | None = None,
) -> bool:
    """Set a forward's status; with `only_from`, only when it still has that status."""
    query = update(WorkerForward).where(WorkerForward.id == forward_id)
    if only_from is not None:
        query = query.where(WorkerForward.status == only_from)
    result = await db.execute(
        query.values(
            status=status,
            code=code,
            message=message,
            http_status=http_status,
            updated_at=utc_now(),
        ).execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0))


async def list_pending_worker_forwards(
    db: AsyncSession, *, target: str, limit: int = 100
) -> list[uuid.UUID]:
    result = await db.scalars(
        select(WorkerForward.id)
        .where(WorkerForward.target == target, WorkerForward.status == "pending")
        .order_by(WorkerForward.created_at)
        .limit(limit)
    )
    return list(result)


async def delete_worker_forward(db: AsyncSession, forward_id: uuid.UUID) -> None:
    await db.execute(delete(WorkerForward).where(WorkerForward.id == forward_id))


async def purge_worker_forwards(db: AsyncSession, older_than: datetime) -> int:
    result = await db.execute(
        delete(WorkerForward).where(WorkerForward.created_at < older_than)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def clear_worker_api_instance(
    db: AsyncSession, worker_id: uuid.UUID, *, instance_id: str | None
) -> None:
    row = await get_worker(db, worker_id)
    if row is None:
        return
    if row.api_instance_id == instance_id:
        row.api_instance_id = None
        await db.flush()


async def create_worker_token(
    db: AsyncSession,
    *,
    name: str,
    token_hash: str,
    worker_id: uuid.UUID | None = None,
) -> WorkerToken:
    row = WorkerToken(name=name, token_hash=token_hash, worker_id=worker_id)
    db.add(row)
    await db.flush()
    return row


async def list_worker_tokens(db: AsyncSession) -> list[WorkerToken]:
    result = await db.scalars(select(WorkerToken).order_by(WorkerToken.created_at))
    return list(result)


async def get_worker_token(db: AsyncSession, token_id: uuid.UUID) -> WorkerToken | None:
    return await db.scalar(select(WorkerToken).where(WorkerToken.id == token_id))


async def find_worker_token(db: AsyncSession, token_hash: str) -> WorkerToken | None:
    return await db.scalar(
        select(WorkerToken).where(WorkerToken.token_hash == token_hash)
    )


async def bind_worker_token(
    db: AsyncSession, row: WorkerToken, worker_id: uuid.UUID
) -> WorkerToken:
    row.worker_id = worker_id
    await db.flush()
    return row


async def touch_worker_token(db: AsyncSession, row: WorkerToken) -> WorkerToken:
    row.last_used_at = utc_now()
    await db.flush()
    return row


async def revoke_worker_token(
    db: AsyncSession, token_id: uuid.UUID
) -> WorkerToken | None:
    row = await get_worker_token(db, token_id)
    if row is None or row.revoked_at is not None:
        return row
    row.revoked_at = utc_now()
    await db.flush()
    return row


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
    db: AsyncSession,
    worker_id: uuid.UUID,
    *,
    lease_until: datetime,
    generation: int | None = None,
) -> None:
    """Extend every lease of the worker; a superseded `generation` extends none."""
    statement = update(SessionRow).where(
        SessionRow.worker_id == worker_id,
        SessionRow.lease_id.is_not(None),
    )
    if generation is not None:
        statement = statement.where(
            select(WorkerRow.generation)
            .where(WorkerRow.id == worker_id)
            .scalar_subquery()
            == generation
        )
    await db.execute(
        statement.values(
            lease_until=lease_until, updated_at=utc_now()
        ).execution_options(synchronize_session=False)
    )


async def renew_session_leases(
    db: AsyncSession,
    *,
    worker_id: uuid.UUID,
    lease_ids: Collection[uuid.UUID],
    lease_until: datetime,
) -> int:
    """Renew exactly the claimed leases; returns the renewed row count.

    The conditional UPDATE matches `worker_id` and `lease_id`, so a
    reconnecting worker takes over only the leases it still reports:
    rows granted elsewhere (or since re-granted) keep their cursor.
    """
    ids = list(lease_ids)
    if not ids:
        return 0
    result = await db.execute(
        update(SessionRow)
        .where(
            SessionRow.worker_id == worker_id,
            SessionRow.lease_id.in_(ids),
        )
        .values(lease_until=lease_until, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


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
    mcp_server_url: str | None = None,
    token: str,
    credential_id: uuid.UUID | None = None,
    secret_name: str | None = None,
    allowed_hosts: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> VaultCredential:
    row = VaultCredential(
        id=credential_id if credential_id is not None else uuid.uuid4(),
        tenant_id=tenant_id,
        vault_id=vault_id,
        name=name,
        auth_type=auth_type,
        mcp_server_url=mcp_server_url,
        token=token,
        secret_name=secret_name,
        allowed_hosts=allowed_hosts,
        metadata_json=metadata if metadata is not None else {},
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
    metadata: dict[str, Any] | None = None,
) -> VaultCredential | None:
    row = await get_vault_credential(db, tenant_id, vault_id, credential_id)
    if row is None:
        return None
    if name is not None:
        row.name = name
    if token is not None:
        row.token = token
    if metadata is not None:
        row.metadata_json = metadata
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
    kind: str = "file",
    user_id: str | None = None,
) -> FileRow:
    row = FileRow(
        id=file_id,
        tenant_id=tenant_id,
        filename=filename,
        purpose=purpose,
        kind=kind,
        user_id=user_id,
        size=size,
        content_type=content_type,
    )
    db.add(row)
    await db.flush()
    return row


USER_FILE_KINDS = ("attachment", "image")


def file_visible(user_id: str | None) -> list[Any]:
    """The rule for which files a caller with `user_id` can see.

    Files of kind `attachment` or `image` are user files. With a
    `user_id`, a user file is visible only when it has the same
    `user_id` or none. Other kinds and callers without a `user_id`
    stay tenant-scoped.
    """
    if user_id is None:
        return []
    return [
        or_(
            FileRow.kind.not_in(USER_FILE_KINDS),
            FileRow.user_id.is_(None),
            FileRow.user_id == user_id,
        )
    ]


async def get_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: str,
    *,
    user_id: str | None = None,
) -> FileRow | None:
    return await db.scalar(
        select(FileRow).where(
            FileRow.tenant_id == tenant_id,
            FileRow.id == file_id,
            *file_visible(user_id),
        )
    )


def _after(
    created: Any, key: Any, cursor: tuple[datetime, str] | None, order: str
) -> list[Any]:
    if cursor is None:
        return []
    at, last = cursor
    if order == "asc":
        return [or_(created > at, and_(created == at, key > last))]
    return [or_(created < at, and_(created == at, key < last))]


def _order(created: Any, key: Any, order: str) -> tuple[Any, Any]:
    if order == "asc":
        return created.asc(), key.asc()
    return created.desc(), key.desc()


async def file_cursor(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: str,
    *,
    user_id: str | None = None,
) -> tuple[datetime, str] | None:
    created = await db.scalar(
        select(FileRow.created_at).where(
            FileRow.tenant_id == tenant_id,
            FileRow.id == file_id,
            *file_visible(user_id),
        )
    )
    return None if created is None else (created, file_id)


async def list_files(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kinds: Collection[str] | None = None,
    purpose: str | None = None,
    owner_id: str | None = None,
    session_id: uuid.UUID | None = None,
    filename_prefix: str | None = None,
    ids: Collection[str] | None = None,
    after: tuple[datetime, str] | None = None,
    order: str = "desc",
    limit: int | None = None,
    user_id: str | None = None,
) -> tuple[list[FileRow], bool]:
    """One page of the tenant's files, newest first unless `order` is `asc`.

    Returns the rows and whether more rows follow. `after` is the
    `(created_at, id)` of the last row of the previous page. `owner_id`
    filters by the file's `user_id`. `user_id` is the caller.
    """
    query = select(FileRow).where(FileRow.tenant_id == tenant_id)
    query = query.where(*file_visible(user_id))
    if kinds is not None:
        query = query.where(FileRow.kind.in_(list(kinds)))
    if purpose is not None:
        query = query.where(FileRow.purpose == purpose)
    if owner_id is not None:
        query = query.where(FileRow.user_id == owner_id)
    if filename_prefix:
        query = query.where(
            func.substr(FileRow.filename, 1, len(filename_prefix)) == filename_prefix
        )
    if ids is not None:
        query = query.where(FileRow.id.in_(list(ids)))
    if session_id is not None:
        query = query.where(
            exists().where(
                SessionFileRow.tenant_id == tenant_id,
                SessionFileRow.session_id == session_id,
                SessionFileRow.file_id == FileRow.id,
            )
        )
    query = query.where(*_after(FileRow.created_at, FileRow.id, after, order))
    query = query.order_by(*_order(FileRow.created_at, FileRow.id, order))
    if limit is not None:
        query = query.limit(limit + 1)
    rows = list(await db.scalars(query))
    if limit is not None and len(rows) > limit:
        return rows[:limit], True
    return rows, False


async def delete_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: str,
    *,
    user_id: str | None = None,
) -> bool:
    row = await get_file(db, tenant_id, file_id, user_id=user_id)
    if row is None:
        return False
    await db.execute(
        delete(SessionFileRow).where(
            SessionFileRow.tenant_id == tenant_id, SessionFileRow.file_id == file_id
        )
    )
    await db.delete(row)
    await db.flush()
    return True


async def promote_attachments(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    file_ids: Collection[str],
    *,
    user_id: str | None = None,
    kinds: Collection[str] = ("attachment",),
) -> None:
    """Make files of `kinds` used as agent or session input files of kind `file`.

    Only files the caller with `user_id` can see are changed.
    """
    if not file_ids:
        return
    await db.execute(
        update(FileRow)
        .where(
            FileRow.tenant_id == tenant_id,
            FileRow.id.in_(list(file_ids)),
            FileRow.kind.in_(list(kinds)),
            *file_visible(user_id),
        )
        .values(kind="file")
    )


async def bind_session_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    file_id: str,
    *,
    path: str | None = None,
    item_id: uuid.UUID | None = None,
) -> SessionFileRow:
    """Bind a file to a session. A file already bound keeps its binding."""
    row = await db.get(SessionFileRow, (tenant_id, session_id, file_id))
    if row is not None:
        return row
    row = SessionFileRow(
        tenant_id=tenant_id,
        session_id=session_id,
        file_id=file_id,
        path=path,
        item_id=item_id,
    )
    db.add(row)
    await db.flush()
    return row


async def lock_session(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> SessionRow | None:
    """Read a session row and lock it until the transaction ends (Postgres)."""
    return await db.scalar(
        select(SessionRow)
        .where(SessionRow.tenant_id == tenant_id, SessionRow.id == session_id)
        .with_for_update()
    )


async def lock_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: str,
    *,
    user_id: str | None = None,
) -> FileRow | None:
    """Read a file row the caller can see and lock it (Postgres)."""
    return await db.scalar(
        select(FileRow)
        .where(
            FileRow.tenant_id == tenant_id,
            FileRow.id == file_id,
            *file_visible(user_id),
        )
        .with_for_update()
    )


async def unbind_files(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    file_ids: Collection[str],
) -> None:
    """Delete the bindings of these files to one session; the files stay."""
    if not file_ids:
        return
    await db.execute(
        delete(SessionFileRow).where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
            SessionFileRow.file_id.in_(list(file_ids)),
        )
    )


async def clear_paths(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    file_ids: Collection[str],
) -> None:
    """Clear the workspace path of these files' bindings to one session."""
    if not file_ids:
        return
    await db.execute(
        update(SessionFileRow)
        .where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
            SessionFileRow.file_id.in_(list(file_ids)),
        )
        .values(path=None)
    )


async def link_session_file_item(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    file_ids: Collection[str],
    item_id: uuid.UUID,
) -> None:
    """Set the user item of bound files that have none yet."""
    if not file_ids:
        return
    await db.execute(
        update(SessionFileRow)
        .where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
            SessionFileRow.file_id.in_(list(file_ids)),
            SessionFileRow.item_id.is_(None),
        )
        .values(item_id=item_id)
    )


async def session_file_cursor(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    file_id: str,
    *,
    user_id: str | None = None,
) -> tuple[datetime, str] | None:
    created = await db.scalar(
        select(SessionFileRow.created_at)
        .join(
            FileRow,
            and_(
                FileRow.tenant_id == SessionFileRow.tenant_id,
                FileRow.id == SessionFileRow.file_id,
            ),
        )
        .where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
            SessionFileRow.file_id == file_id,
            *file_visible(user_id),
        )
    )
    return None if created is None else (created, file_id)


async def list_session_files(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    ids: Collection[str] | None = None,
    after: tuple[datetime, str] | None = None,
    order: str = "asc",
    limit: int | None = None,
    user_id: str | None = None,
) -> tuple[list[tuple[SessionFileRow, FileRow]], bool]:
    """The files bound to a session with their bindings, in binding order.

    `user_id` is the caller; files it cannot see are left out.
    """
    query = (
        select(SessionFileRow, FileRow)
        .join(
            FileRow,
            and_(
                FileRow.tenant_id == SessionFileRow.tenant_id,
                FileRow.id == SessionFileRow.file_id,
            ),
        )
        .where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
            *file_visible(user_id),
        )
    )
    if ids is not None:
        query = query.where(SessionFileRow.file_id.in_(list(ids)))
    created, key = SessionFileRow.created_at, SessionFileRow.file_id
    query = query.where(*_after(created, key, after, order))
    query = query.order_by(*_order(created, key, order))
    if limit is not None:
        query = query.limit(limit + 1)
    rows = [(binding, file) for binding, file in (await db.execute(query)).all()]
    if limit is not None and len(rows) > limit:
        return rows[:limit], True
    return rows, False


async def unbind_session_files(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[str]:
    """Delete the session's bindings and the files only this session used.

    Only files of kind `attachment` or `image` are deleted. Returns the
    ids of the deleted files so the caller can delete their bytes.
    """
    other = aliased(SessionFileRow)
    owned = list(
        await db.scalars(
            select(FileRow.id)
            .join(
                SessionFileRow,
                and_(
                    SessionFileRow.tenant_id == FileRow.tenant_id,
                    SessionFileRow.file_id == FileRow.id,
                ),
            )
            .where(
                SessionFileRow.tenant_id == tenant_id,
                SessionFileRow.session_id == session_id,
                FileRow.kind.in_(USER_FILE_KINDS),
                ~exists().where(
                    other.tenant_id == tenant_id,
                    other.file_id == FileRow.id,
                    other.session_id != session_id,
                ),
            )
        )
    )
    await db.execute(
        delete(SessionFileRow).where(
            SessionFileRow.tenant_id == tenant_id,
            SessionFileRow.session_id == session_id,
        )
    )
    if owned:
        await db.execute(
            delete(FileRow).where(FileRow.tenant_id == tenant_id, FileRow.id.in_(owned))
        )
    await db.flush()
    return owned


async def delete_unbound_attachments(
    db: AsyncSession, *, before: datetime, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """Delete attachments created before `before` that no session uses.

    Each row is locked before its delete, so on Postgres the delete waits
    for a bind that holds the row and then sees the new binding.
    """
    unbound = ~exists().where(
        SessionFileRow.tenant_id == FileRow.tenant_id,
        SessionFileRow.file_id == FileRow.id,
    )
    found = (
        await db.execute(
            select(FileRow.tenant_id, FileRow.id)
            .where(
                FileRow.kind == "attachment",
                FileRow.created_at < before,
                unbound,
            )
            .order_by(FileRow.created_at)
            .limit(limit)
        )
    ).all()
    deleted: list[tuple[uuid.UUID, str]] = []
    for tenant_id, file_id in sorted(found, key=lambda row: (str(row[0]), row[1])):
        await db.execute(
            select(FileRow.id)
            .where(FileRow.tenant_id == tenant_id, FileRow.id == file_id)
            .with_for_update()
        )
        result = await db.execute(
            delete(FileRow).where(
                FileRow.tenant_id == tenant_id, FileRow.id == file_id, unbound
            )
        )
        if getattr(result, "rowcount", 0):
            deleted.append((tenant_id, file_id))
    await db.flush()
    return deleted


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


async def create_template(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    template_id: str,
    created_by: str | None,
    name: str | None,
    description: str | None,
    schema_version: str,
    object_id: str,
    size: int,
    sha256: str,
) -> TemplateRow:
    row = TemplateRow(
        id=template_id,
        tenant_id=tenant_id,
        created_by=created_by,
        name=name,
        description=description,
        schema_version=schema_version,
        visibility="tenant",
        object_id=object_id,
        size=size,
        sha256=sha256,
    )
    db.add(row)
    await db.flush()
    return row


async def get_template(
    db: AsyncSession, tenant_id: uuid.UUID, template_id: str
) -> TemplateRow | None:
    return await db.scalar(
        select(TemplateRow).where(
            TemplateRow.tenant_id == tenant_id, TemplateRow.id == template_id
        )
    )


async def list_templates(db: AsyncSession, tenant_id: uuid.UUID) -> list[TemplateRow]:
    result = await db.scalars(
        select(TemplateRow)
        .where(TemplateRow.tenant_id == tenant_id)
        .order_by(TemplateRow.created_at)
    )
    return list(result)


async def delete_template(
    db: AsyncSession, tenant_id: uuid.UUID, template_id: str
) -> bool:
    row = await get_template(db, tenant_id, template_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def get_credential_by_id(
    db: AsyncSession, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> VaultCredential | None:
    return await db.scalar(
        select(VaultCredential).where(
            VaultCredential.tenant_id == tenant_id,
            VaultCredential.id == credential_id,
        )
    )


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
    user_id: str | None = None,
    upload_id: uuid.UUID | None = None,
) -> UploadRow:
    row = UploadRow(
        id=upload_id if upload_id is not None else uuid.uuid4(),
        tenant_id=tenant_id,
        purpose=purpose,
        object_id=object_id,
        filename=filename,
        content_type=content_type,
        declared_bytes=declared_bytes,
        user_id=user_id,
        status="pending",
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def get_upload(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    upload_id: uuid.UUID,
    *,
    user_id: str | None = None,
) -> UploadRow | None:
    """An upload of the tenant.

    With `user_id`, only an upload of that user or of no user.
    """
    query = select(UploadRow).where(
        UploadRow.tenant_id == tenant_id, UploadRow.id == upload_id
    )
    if user_id is not None:
        query = query.where(
            or_(UploadRow.user_id.is_(None), UploadRow.user_id == user_id)
        )
    return await db.scalar(query)


async def create_artifact_upload(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    kind: str,
    filename: str,
    content_type: str,
    declared_bytes: int,
    sha256: str | None,
    expires_at: datetime,
    request_id: uuid.UUID | None = None,
) -> ArtifactUploadRow:
    row = ArtifactUploadRow(
        request_id=request_id,
        tenant_id=tenant_id,
        session_id=session_id,
        artifact_id=artifact_id,
        kind=kind,
        filename=filename,
        content_type=content_type,
        declared_bytes=declared_bytes,
        sha256=sha256,
        status="pending",
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def get_artifact_upload_by_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    request_id: uuid.UUID,
) -> ArtifactUploadRow | None:
    return await db.scalar(
        select(ArtifactUploadRow).where(
            ArtifactUploadRow.tenant_id == tenant_id,
            ArtifactUploadRow.session_id == session_id,
            ArtifactUploadRow.request_id == request_id,
        )
    )


async def get_artifact_upload(
    db: AsyncSession, tenant_id: uuid.UUID, upload_id: uuid.UUID
) -> ArtifactUploadRow | None:
    return await db.scalar(
        select(ArtifactUploadRow).where(
            ArtifactUploadRow.tenant_id == tenant_id,
            ArtifactUploadRow.id == upload_id,
        )
    )
