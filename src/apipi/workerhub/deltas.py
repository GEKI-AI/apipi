from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from apipi.common.event_bus import EventBus, live_event_body
from apipi.common.ratelimit import relay_rate_allowed
from apipi.protocol import (
    DELTA_MAX_TEXT,
    DELTA_RATE_LIMIT,
    WorkerEnvelope,
)
from apipi.store.engine import Store
from apipi.store.repo import (
    get_session_by_id,
    list_events,
)
from apipi.workerhub.connection import WorkerConnection

if TYPE_CHECKING:
    from apipi.workerhub.hub import WorkerHub

log = logging.getLogger("apipi.worker")

DELTA_DONE_TYPE = "agent.session.turn.output_text.done"
DELTA_TERMINAL_TYPES = frozenset(
    {
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
    }
)
_DELTA_DONE_CAP = 256
DELTA_LEASE_REFRESH = 30.0


def _done_turns_in(events: list[Any]) -> set[str]:
    """Collect turn ids whose final text already committed.

    A delta for one of these turns is stale: the final item is the
    source of truth, so the delta is dropped."""
    done: set[str] = set()
    for event in events:
        data = event.data
        if not isinstance(data, dict):
            continue
        turn_id = data.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if event.type == DELTA_DONE_TYPE or event.type in DELTA_TERMINAL_TYPES:
            done.add(turn_id)
    return done


@dataclass
class DeltaLease:
    """In-memory fast path for delta validation.

    The replica holding the socket already knows which lease each
    session holds, so most deltas skip the lease-row read. ``last_seq``
    is the newest stored event seq seen so far; the drop-after-done
    check only reads events after it. ``done`` caches turns already
    known done, so those deltas need no read at all."""

    worker_id: uuid.UUID
    lease_id: uuid.UUID
    tenant_id: uuid.UUID
    last_seq: int = 0
    done: set[str] = field(default_factory=set)
    refreshed: float = field(default_factory=time.monotonic)


def _note_delta_lease(
    hub: WorkerHub,
    session_id: uuid.UUID,
    *,
    worker_id: uuid.UUID,
    lease_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> DeltaLease:
    """Record the live lease so deltas skip the lease-row read."""
    known = hub._delta_leases.get(session_id)
    if known is None:
        known = DeltaLease(worker_id=worker_id, lease_id=lease_id, tenant_id=tenant_id)
        hub._delta_leases[session_id] = known
        return known
    known.worker_id = worker_id
    known.lease_id = lease_id
    known.tenant_id = tenant_id
    known.refreshed = time.monotonic()
    return known


def _forget_delta(hub: WorkerHub, session_id: uuid.UUID) -> None:
    """Drop per-session delta state when its lease ends."""
    hub._delta_hits.pop(session_id, None)
    hub._delta_leases.pop(session_id, None)


def _forget_worker_deltas(hub: WorkerHub, worker_id: uuid.UUID) -> None:
    """Drop per-session delta state when a connection closes."""
    for session_id in [
        session_id
        for session_id, known in hub._delta_leases.items()
        if known.worker_id == worker_id
    ]:
        hub._forget_delta(session_id)
    hub._inventory.pop(worker_id, None)


async def handle_delta(
    hub: WorkerHub,
    store: Store,
    bus: EventBus,
    conn: WorkerConnection,
    envelope: WorkerEnvelope,
) -> bool:
    """Validate one ephemeral envelope and fan out its delta.

    Reasoning deltas are accepted but never published: thinking
    deltas are not sent to clients. Text deltas need a live lease
    on this connection, must fit the size and rate budgets, and
    are dropped when the turn already committed its final text.
    The lease check reads from the socket's in-memory lease
    state and only falls back to the lease row when that state
    cannot answer; the drop-after-done check only reads events
    stored after the last check, and cached done turns need no
    read at all. Accepted deltas are published as ``live`` bus
    messages and are never written to the store. Returns whether
    a delta was published."""
    if envelope.type == "delta.reasoning":
        hub.observe_protocol("delta.reasoning_dropped")
        return False
    if envelope.type != "delta.text":
        return False
    try:
        payload = envelope.parsed_payload()
    except ValueError:
        hub.observe_protocol("delta.invalid")
        return False
    text = getattr(payload, "text", "")
    turn_id = getattr(payload, "turn_id", None)
    if not isinstance(text, str) or not text:
        return False
    if len(text) > DELTA_MAX_TEXT:
        hub.observe_protocol("delta.oversize")
        conn.warnings.warning(
            "worker delta oversize",
            event="worker.delta.oversize",
            error_code="delta_oversize",
            worker_id=conn.worker_id,
            session_id=envelope.session_id,
        )
        return False
    if not relay_rate_allowed(
        hub._delta_hits.setdefault(envelope.session_id, []),
        now=time.monotonic(),
        limit=DELTA_RATE_LIMIT,
    ):
        hub.observe_protocol("delta.rate_limited")
        conn.warnings.warning(
            "worker delta rate limited",
            event="worker.delta.rate_limited",
            error_code="delta_rate_limited",
            worker_id=conn.worker_id,
            session_id=envelope.session_id,
        )
        return False
    known = hub._delta_leases.get(envelope.session_id)
    if (
        known is None
        or known.worker_id != conn.worker_id
        or known.lease_id not in conn.leases
        or time.monotonic() - known.refreshed > DELTA_LEASE_REFRESH
    ):
        known = await _refresh_delta_lease(hub, store, conn, envelope.session_id)
        if known is None:
            hub._forget_delta(envelope.session_id)
            hub.observe_protocol("delta.rejected")
            conn.warnings.warning(
                "worker delta for unleased session",
                event="worker.delta.rejected",
                error_code="delta_not_leased",
                worker_id=conn.worker_id,
                session_id=envelope.session_id,
            )
            return False
    if turn_id is not None and str(turn_id) in known.done:
        hub.observe_protocol("delta.dropped_done")
        return False
    if turn_id is not None and await _note_done_turns(
        hub, store, known, envelope.session_id, turn_id
    ):
        hub.observe_protocol("delta.dropped_done")
        return False
    await bus.publish(
        envelope.session_id,
        live_event_body(
            envelope.session_id,
            type="agent.session.turn.output_text.delta",
            data={"delta": text, "turn_id": str(turn_id)},
        ),
    )
    hub.observe_protocol("delta.accepted")
    return True


async def _refresh_delta_lease(
    hub: WorkerHub, store: Store, conn: WorkerConnection, session_id: uuid.UUID
) -> DeltaLease | None:
    """Re-read the lease row when memory cannot validate a delta."""
    async with store.session() as db:
        row = await get_session_by_id(db, session_id)
    if (
        row is None
        or row.worker_id != conn.worker_id
        or row.lease_id not in conn.leases
        or row.lease_id is None
    ):
        return None
    return hub._note_delta_lease(
        session_id,
        worker_id=conn.worker_id,
        lease_id=row.lease_id,
        tenant_id=row.tenant_id,
    )


async def _note_done_turns(
    hub: WorkerHub,
    store: Store,
    known: DeltaLease,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> bool:
    """Record newly committed final turns; say whether this one is done.

    Only events stored after the last check are read, so the
    per-delta cost stays bounded no matter how long the session
    history grows."""
    async with store.session() as db:
        events = await list_events(
            db,
            known.tenant_id,
            session_id,
            after_seq=known.last_seq or None,
        )
    for event in events:
        if event.seq > known.last_seq:
            known.last_seq = event.seq
    for done_turn in _done_turns_in(events):
        if len(known.done) >= _DELTA_DONE_CAP:
            known.done.clear()
        known.done.add(done_turn)
    return str(turn_id) in known.done
