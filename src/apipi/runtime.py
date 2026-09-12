import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store.events import append_event
from apipi.store.models import Event, utc_now
from apipi.store.repo import create_item, create_turn, get_session, update_session

PUBLIC_EVENT_TYPES = frozenset(
    {
        "agent.session.created",
        "agent.session.in_progress",
        "agent.session.idle",
        "agent.session.requires_action",
        "agent.session.failed",
        "agent.session.error",
        "agent.session.turn.created",
        "agent.session.turn.in_progress",
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
        "agent.session.turn.output_text.delta",
        "agent.session.turn.output_text.done",
        "agent.session.turn.item.added",
        "agent.session.turn.item.done",
        "agent.session.environment.pending",
        "agent.session.environment.connected",
        "agent.session.environment.disconnected",
        "agent.session.environment.failed",
    }
)


def event_body(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "type": event.type,
        "seq": event.seq,
        "session_id": str(event.session_id),
        "created_at": event.created_at.isoformat(),
        "data": event.data,
    }


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, list[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(
        self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        subs = self._subs.get(session_id)
        if subs is None:
            return
        if queue in subs:
            subs.remove(queue)
        if not subs:
            del self._subs[session_id]

    def publish(self, session_id: uuid.UUID, event: dict[str, Any]) -> None:
        for queue in list(self._subs.get(session_id, ())):
            queue.put_nowait(event)


class FakeHarness:
    def complete(self, text: str) -> str:
        return text if text else "ok"


async def persist_event(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
) -> Event | None:
    if type not in PUBLIC_EVENT_TYPES:
        return None
    event = await append_event(db, tenant_id, session_id, type=type, data=data)
    hub.publish(session_id, event_body(event))
    return event


async def run_turn(
    db: AsyncSession,
    hub: EventHub,
    harness: FakeHarness,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    text: str,
) -> None:
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        return
    reply = harness.complete(text)
    await update_session(db, tenant_id, session_id, changes={"status": "in_progress"})
    await persist_event(
        db, hub, tenant_id, session_id, type="agent.session.in_progress"
    )
    turn = await create_turn(db, tenant_id, session_id, status="in_progress")
    turn_id = str(turn.id)
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.created",
        data={"turn_id": turn_id},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.in_progress",
        data={"turn_id": turn_id},
    )
    user_item = await create_item(
        db,
        tenant_id,
        session_id,
        type="message",
        turn_id=turn.id,
        data={"role": "user", "content": text},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.added",
        data={"item_id": str(user_item.id), "item_type": "message"},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.done",
        data={"item_id": str(user_item.id)},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.output_text.delta",
        data={"delta": reply, "turn_id": turn_id},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.output_text.done",
        data={"text": reply, "turn_id": turn_id},
    )
    assistant_item = await create_item(
        db,
        tenant_id,
        session_id,
        type="message",
        turn_id=turn.id,
        data={"role": "assistant", "content": reply},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.added",
        data={"item_id": str(assistant_item.id), "item_type": "message"},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.done",
        data={"item_id": str(assistant_item.id)},
    )
    turn.status = "completed"
    turn.updated_at = utc_now()
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.completed",
        data={"turn_id": turn_id},
    )
    await update_session(db, tenant_id, session_id, changes={"status": "idle"})
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")
