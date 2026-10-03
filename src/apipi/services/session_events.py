import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.event_bus import live_event_body
from apipi.protocol import LIVE_EVENT_TYPES, PUBLIC_EVENT_TYPES
from apipi.store.engine import after_commit
from apipi.store.models import Event


def event_body(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "type": event.type,
        "seq": event.seq,
        "session_id": str(event.session_id),
        "created_at": event.created_at.isoformat(),
        "data": event.data,
    }


async def persist_event(
    db: AsyncSession,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
) -> Event | None:
    from apipi.store.repo import append_event

    if type not in PUBLIC_EVENT_TYPES:
        return None
    if type in LIVE_EVENT_TYPES:
        await hub.publish(session_id, live_event_body(session_id, type=type, data=data))
        return None
    event = await append_event(db, tenant_id, session_id, type=type, data=data)
    body = event_body(event)

    async def _publish() -> None:
        await hub.publish(session_id, body)

    after_commit(db, _publish)
    return event
