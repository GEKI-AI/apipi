import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.event_bus import EventBus
from apipi.common.failures import (
    error_mode,
    failure_for,
    session_error_data,
    session_failed_data,
    turn_failed_data,
)
from apipi.protocol import PUBLIC_EVENT_TYPES as PUBLIC_EVENT_TYPES
from apipi.services.session_events import event_body as event_body
from apipi.services.session_events import persist_event as persist_event
from apipi.services.turn_log import _write_turn_log
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import (
    get_session,
    get_session_turn,
    list_turns,
    update_session,
)


def lease_live(until: datetime | None) -> bool:
    if until is None:
        return False
    current = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    return current > utc_now()


async def fail_stale_in_progress(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    message: str = "Turn interrupted",
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id)
    if row is None or row.status != "in_progress":
        return row
    if lease_live(row.lease_until):
        return row
    turns = await list_turns(db, tenant_id, session_id) or []
    for turn in reversed(turns):
        if turn.status == "in_progress":
            await _fail_turn_in_db(
                db,
                hub,
                tenant_id,
                session_id,
                turn.id,
                message,
                code="turn_interrupted",
            )
            return await get_session(db, tenant_id, session_id)
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")
    return await get_session(db, tenant_id, session_id)


async def _fail_turn_in_db(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    message: str,
    *,
    code: str,
) -> None:
    resolved = failure_for(code, message)
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "failed"
        turn.updated_at = utc_now()
        await db.flush()
    await _write_turn_log(
        db,
        tenant_id,
        session_id,
        turn_id,
        status="failed",
        error_code=resolved.code,
        failure=resolved,
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.failed",
        data=turn_failed_data(str(turn_id), resolved),
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_error_data(resolved, mode=error_mode(None)),
    )
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


async def fail_session(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "failed", "required_actions": []},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_failed_data(message, code),
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.failed")


async def fail_environment(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    from apipi.services.sandbox_status import note_failed

    data = await note_failed(db, hub, tenant_id, session_id, message, code=code)
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.environment.failed",
        data=data,
    )
    await fail_session(db, hub, tenant_id, session_id, message, code=code)
