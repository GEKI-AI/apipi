import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from apipi.config import (
    ConfigError,
    Settings,
    load_worker_token,
    reject_legacy_worker_token,
)
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.otel import (
    Tracing,
    attach_traceparent,
    detach_traceparent,
    start_span,
)
from apipi.services.event_bus import EventBus
from apipi.services.failures import (
    failure_for,
    log_extra,
    log_level_for_code,
    session_error_data,
    turn_failed_data,
)
from apipi.services.runtime import (
    PUBLIC_EVENT_TYPES,
    fail_session,
    live_event_body,
    persist_event,
)
from apipi.store.engine import Store
from apipi.store.models import WorkerToken, utc_now
from apipi.store.repo import (
    bind_worker_token,
    clear_session_lease,
    extend_worker_leases,
    get_session,
    get_session_by_id,
    get_session_by_lease,
    get_worker_token,
    list_events,
    list_expired_leases,
    list_worker_leases,
    renew_session_leases,
    set_session_lease,
    touch_worker,
    upsert_worker,
)
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus, relay_rate_allowed
from apipi.worker.outbox import Outbox
from apipi.worker.pi.sandbox import mem_mib_for_size, sandbox_size_of
from apipi.worker.placement import placement_for, worker_accepts
from apipi.worker.protocol import (
    COMMAND_OPS,
    HelloReply,
    RegisterMessage,
    WorkerEnvelope,
)
from apipi.worker.turn_context import (
    COMMAND_CONTEXT_OPS,
    CommandTooLarge,
    ContextBytes,
    check_command_size,
    parse_turn_context,
    summarize_context,
)

WORKER_IN = frozenset(
    {
        "register",
        "heartbeat",
        "lease.ack",
        "lease.release",
        "event",
        "store.proof",
        "inventory",
        "sandbox.seen",
    }
)
DELTA_RATE_LIMIT = 100
DELTA_MAX_TEXT = 32_768
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


log = logging.getLogger("apipi.worker")


@dataclass
class WorkerImage:
    id: str
    version: str
    digest: str
    min_size: str


@dataclass
class _DeltaLease:
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


@dataclass
class WorkerConnection:
    worker_id: uuid.UUID
    generation: int
    websocket: WebSocket
    capacity: int
    memory_mb: int
    run_mode: str
    token_id: uuid.UUID | None = None
    arch: str = ""
    leases: set[uuid.UUID] = field(default_factory=set)
    lease_mem: dict[uuid.UUID, int] = field(default_factory=dict)
    draining: bool = False
    images: dict[str, WorkerImage] = field(default_factory=dict)
    accepts: frozenset[str] = frozenset({"none"})
    store_proof: tuple[str, str] | None = None


class WorkerHub:
    def __init__(
        self,
        settings: Settings,
        *,
        metrics: Any | None = None,
        tracing: Tracing | None = None,
    ) -> None:
        self.settings = settings
        self.metrics = metrics
        self.tracing = tracing
        self._conns: dict[uuid.UUID, WorkerConnection] = {}
        self._unacked: dict[uuid.UUID, dict[str, Any]] = {}
        self._delta_hits: dict[uuid.UUID, list[float]] = {}
        self._delta_leases: dict[uuid.UUID, _DeltaLease] = {}
        self._inventory: dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]] = {}
        self._metric_modes: set[str] = set()
        self._lock = asyncio.Lock()

    def live(self) -> int:
        return len(self._conns)

    def observe_protocol(self, event: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_protocol(event)

    def get(self, worker_id: uuid.UUID) -> WorkerConnection | None:
        return self._conns.get(worker_id)

    async def attach(self, conn: WorkerConnection) -> WorkerConnection | None:
        async with self._lock:
            previous = self._conns.get(conn.worker_id)
            self._conns[conn.worker_id] = conn
        if previous is not None and previous is not conn:
            await _close(previous.websocket)
        self._observe()
        return conn

    async def detach(
        self, worker_id: uuid.UUID, conn: WorkerConnection | None = None
    ) -> None:
        async with self._lock:
            current = self._conns.get(worker_id)
            if current is None:
                return
            if conn is not None and current is not conn:
                return
            del self._conns[worker_id]
        self._observe()
        self._forget_worker_deltas(worker_id)

    def _note_delta_lease(
        self,
        session_id: uuid.UUID,
        *,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> _DeltaLease:
        """Record the live lease so deltas skip the lease-row read."""
        known = self._delta_leases.get(session_id)
        if known is None:
            known = _DeltaLease(
                worker_id=worker_id, lease_id=lease_id, tenant_id=tenant_id
            )
            self._delta_leases[session_id] = known
            return known
        known.worker_id = worker_id
        known.lease_id = lease_id
        known.tenant_id = tenant_id
        known.refreshed = time.monotonic()
        return known

    def _forget_delta(self, session_id: uuid.UUID) -> None:
        """Drop per-session delta state when its lease ends."""
        self._delta_hits.pop(session_id, None)
        self._delta_leases.pop(session_id, None)

    def _forget_worker_deltas(self, worker_id: uuid.UUID) -> None:
        """Drop per-session delta state when a connection closes."""
        for session_id in [
            session_id
            for session_id, known in self._delta_leases.items()
            if known.worker_id == worker_id
        ]:
            self._forget_delta(session_id)
        self._inventory.pop(worker_id, None)

    def note_inventory(
        self, worker_id: uuid.UUID, sessions: dict[uuid.UUID, uuid.UUID]
    ) -> None:
        """Remember the worker's reported live set (session_id to lease_id)."""
        self._inventory[worker_id] = dict(sessions)

    async def owned_sessions(
        self,
        store: Store,
        conn: WorkerConnection,
        session_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Filter seen ids to sessions leased to this connection."""
        if not session_ids:
            return []
        from sqlalchemy import select

        from apipi.store.models import SessionRow

        async with store.session() as db:
            rows = (
                await db.scalars(
                    select(SessionRow).where(SessionRow.id.in_(session_ids))
                )
            ).all()
        return [
            row.id
            for row in rows
            if row.worker_id == conn.worker_id
            and row.lease_id is not None
            and row.lease_id in conn.leases
        ]

    def known_live_sessions(self) -> list[uuid.UUID]:
        """Every session some connected worker reports as live."""
        seen: list[uuid.UUID] = []
        for sessions in self._inventory.values():
            for session_id in sessions:
                if session_id not in seen:
                    seen.append(session_id)
        return seen

    def _observe(self) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        counts: dict[str, int] = {}
        leases: dict[str, int] = {}
        for conn in self._conns.values():
            counts[conn.run_mode] = counts.get(conn.run_mode, 0) + 1
            leases[conn.run_mode] = leases.get(conn.run_mode, 0) + len(conn.leases)
        self._metric_modes |= set(counts)
        self._metric_modes |= {"microvm", "none"}
        metrics.set_workers(counts, leases, modes=self._metric_modes)

    def has_image(self, kind: str, image: str | None) -> bool:
        if image is None or kind != "microvm":
            return True
        return any(
            kind in conn.accepts and image in conn.images
            for conn in self._conns.values()
        )

    def pick(
        self,
        session_mem_mib: int | None = None,
        *,
        kind: str,
        image: str | None = None,
    ) -> WorkerConnection | None:
        required = kind
        session_mem = (
            session_mem_mib
            if session_mem_mib is not None
            else self.settings.microvm_mem_mib
        )
        ready = []
        for conn in self._conns.values():
            if required not in conn.accepts:
                continue
            if image is not None and required == "microvm" and image not in conn.images:
                continue
            if conn.draining:
                continue
            if len(conn.leases) + 1 > conn.capacity:
                continue
            used = sum(conn.lease_mem.get(lease, session_mem) for lease in conn.leases)
            if used + session_mem > conn.memory_mb:
                continue
            ready.append((conn, used))
        if not ready:
            return None
        ready.sort(
            key=lambda item: (-(item[0].memory_mb - item[1]), len(item[0].leases))
        )
        return ready[0][0]

    async def acquire(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        op: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if op not in COMMAND_OPS:
            raise ValueError(op)
        started = time.monotonic()
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        with start_span(
            self.tracing,
            "worker.assign",
            session_id=session_id,
            request_id=request_id,
        ):
            return await self._acquire(
                store,
                tenant_id,
                session_id,
                op=op,
                payload=payload,
                started=started,
            )

    async def _acquire(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        op: str,
        payload: dict[str, Any] | None,
        started: float,
    ) -> dict[str, Any] | None:
        async with store.session() as db:
            session = await get_session(db, tenant_id, session_id)
        if session is None:
            return None
        required = placement_for(environment=session.environment)
        session_mem = mem_mib_for_size(
            self.settings, sandbox_size_of(session.environment)
        )
        image = _session_image(session.environment) if required == "microvm" else None
        kind_live = any(required in conn.accepts for conn in self._conns.values())
        if image is not None and kind_live and not self.has_image(required, image):
            raise ApiError(
                "api_error",
                image_unavailable_message(self, image),
                code="image_unavailable",
                status_code=503,
                session_id=str(session_id),
            )
        conn = self.pick(session_mem, kind=required, image=image)
        if conn is None:
            request_id = (
                payload.get("request_id") if isinstance(payload, dict) else None
            )
            log_event(
                log,
                logging.WARNING,
                "worker assign failed",
                event="worker.assign.failed",
                error_code="capacity",
                tenant_id=tenant_id,
                session_id=session_id,
                request_id=request_id,
            )
            return None
        lease_id = uuid.uuid4()
        command_id = uuid.uuid4()
        until = utc_now() + self.settings.worker_lease_ttl
        command = {
            "type": "command",
            "id": str(command_id),
            "session_id": str(session_id),
            "lease_id": str(lease_id),
            "op": op,
            "payload": _payload_with_image(
                _payload_with_run_mode(payload, required), image
            ),
        }
        _check_command_context(op, command["payload"])
        # Mark the command unacked before the grant commits, so an
        # inventory arriving between the grant and the send does not
        # orphan a lease whose command is still in flight.
        self._unacked[lease_id] = command
        async with store.session() as db:
            row = await set_session_lease(
                db,
                tenant_id,
                session_id,
                worker_id=conn.worker_id,
                lease_id=lease_id,
                lease_until=until,
            )
            if row is None:
                self._unacked.pop(lease_id, None)
                return None
        conn.leases.add(lease_id)
        conn.lease_mem[lease_id] = session_mem
        self._note_delta_lease(
            session_id,
            worker_id=conn.worker_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
        )
        try:
            await _send(conn.websocket, command)
        except Exception:
            self._unacked.pop(lease_id, None)
            raise
        metrics = self.metrics
        if metrics is not None:
            metrics.worker_assign.observe(time.monotonic() - started)
        self._observe()
        return command

    async def command(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        op: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if op not in COMMAND_OPS:
            raise ValueError(op)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None or row.lease_id is None or row.worker_id is None:
                return None
            worker_id = row.worker_id
            lease_id = row.lease_id
            required = placement_for(environment=row.environment)
        conn = self._conns.get(worker_id)
        if conn is None or lease_id not in conn.leases:
            return None
        self._note_delta_lease(
            session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
        )
        follow_image = None
        if required == "microvm" and row is not None:
            follow_image = _session_image(row.environment)
            if follow_image not in conn.images:
                raise ApiError(
                    "api_error",
                    image_unavailable_message(self, follow_image),
                    code="image_unavailable",
                    status_code=503,
                    session_id=str(session_id),
                )
        command_id = uuid.uuid4()
        command = {
            "type": "command",
            "id": str(command_id),
            "session_id": str(session_id),
            "lease_id": str(lease_id),
            "op": op,
            "payload": _payload_with_image(
                _payload_with_run_mode(payload, required), follow_image
            ),
        }
        _check_command_context(op, command["payload"])
        self._unacked[lease_id] = command
        try:
            await _send(conn.websocket, command)
        except Exception:
            self._unacked.pop(lease_id, None)
            raise
        return command

    async def ack(self, lease_id: uuid.UUID, command_id: str) -> bool:
        pending = self._unacked.get(lease_id)
        if pending is None or pending.get("id") != command_id:
            return False
        self._unacked.pop(lease_id, None)
        return True

    async def wait_ack(
        self, lease_id: uuid.UUID, command_id: str, *, timeout: float = 15
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = self._unacked.get(lease_id)
            if pending is None or pending.get("id") != command_id:
                return True
            await asyncio.sleep(0.05)
        log.warning(
            "worker command ack timed out",
            extra={"lease_id": str(lease_id), "command_id": command_id},
        )
        return False

    async def release(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        lease_id: uuid.UUID,
    ) -> None:
        self._unacked.pop(lease_id, None)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                self._forget_delta(session_id)
                return
            if row.lease_id != lease_id:
                return
            worker_id = row.worker_id
            await clear_session_lease(db, tenant_id, session_id)
            self._forget_delta(session_id)
        if worker_id is not None:
            conn = self._conns.get(worker_id)
            if conn is not None:
                conn.leases.discard(lease_id)
                conn.lease_mem.pop(lease_id, None)
        self._observe()

    async def expire(self, store: Store, hub: EventBus) -> list[uuid.UUID]:
        expired: list[uuid.UUID] = []
        async with store.session() as db:
            rows = await list_expired_leases(db, utc_now())
            for row in rows:
                lease_id = row.lease_id
                worker_id = row.worker_id
                await clear_session_lease(db, row.tenant_id, row.id)
                lease_failure = failure_for(
                    "worker_lease_expired", "Worker lease expired"
                )
                log_event(
                    log,
                    logging.ERROR,
                    "worker lease expired",
                    event="worker.lease.expired",
                    tenant_id=row.tenant_id,
                    session_id=row.id,
                    worker_id=worker_id,
                    **log_extra(lease_failure),
                )
                await persist_event(
                    db,
                    hub,
                    row.tenant_id,
                    row.id,
                    type="agent.session.error",
                    data=session_error_data(lease_failure, mode="legacy"),
                )
                expired.append(row.id)
                self._forget_delta(row.id)
                if lease_id is not None:
                    self._unacked.pop(lease_id, None)
                    conn = self._conns.get(worker_id) if worker_id is not None else None
                    if conn is not None:
                        conn.leases.discard(lease_id)
                        conn.lease_mem.pop(lease_id, None)
                        await _send(
                            conn.websocket,
                            {
                                "type": "lease.revoke",
                                "session_id": str(row.id),
                                "lease_id": str(lease_id),
                            },
                        )
        self._observe()
        return expired

    async def replay(
        self,
        conn: WorkerConnection,
        store: Store,
        running: list[Any] | None = None,
    ) -> dict[uuid.UUID, int]:
        sessions = await self.restore_leases(conn, store, running)
        await self.resend_pending(conn)
        return sessions

    async def restore_leases(
        self, conn: WorkerConnection, store: Store, running: list[Any] | None = None
    ) -> dict[uuid.UUID, int]:
        """Reattach the worker's leases and report persisted seq cursors.

        `running` carries the worker's `[{session_id, lease_id}]` claim
        from register. A non-empty claim is verified: a session is only
        reattached when the row still names this worker and lease. An
        empty claim keeps the previous behavior of reattaching every
        lease the row still assigns to this worker. Either way the
        cursor is `sessions.worker_seq` so the worker replays exactly
        what ingest has not persisted.
        """
        from apipi.worker.protocol import RunningSession

        async with store.session() as db:
            rows = await list_worker_leases(db, conn.worker_id)
        claimed: dict[uuid.UUID, uuid.UUID] = {}
        if running:
            for entry in running:
                try:
                    parsed = (
                        entry
                        if isinstance(entry, RunningSession)
                        else RunningSession.model_validate(entry)
                    )
                except Exception:
                    continue
                claimed[parsed.session_id] = parsed.lease_id
        sessions: dict[uuid.UUID, int] = {}
        renewed: list[uuid.UUID] = []
        for row in rows:
            if row.lease_id is None:
                continue
            if claimed and claimed.get(row.id) != row.lease_id:
                continue
            conn.leases.add(row.lease_id)
            conn.lease_mem[row.lease_id] = mem_mib_for_size(
                self.settings, sandbox_size_of(row.environment)
            )
            self._note_delta_lease(
                row.id,
                worker_id=conn.worker_id,
                lease_id=row.lease_id,
                tenant_id=row.tenant_id,
            )
            sessions[row.id] = row.worker_seq
            renewed.append(row.lease_id)
        if renewed:
            # A reconnect to another replica takes the lease over with
            # a conditional UPDATE matching worker and lease, so only
            # the leases the worker still reports move to this replica
            # and running turns stay alive.
            async with store.session() as db:
                await renew_session_leases(
                    db,
                    worker_id=conn.worker_id,
                    lease_ids=renewed,
                    lease_until=utc_now() + self.settings.worker_lease_ttl,
                )
        return sessions

    async def reconcile_inventory(
        self,
        store: Store,
        bus: EventBus,
        worker_id: uuid.UUID,
        reported: dict[uuid.UUID, uuid.UUID],
        unleased: Collection[uuid.UUID] = (),
    ) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
        """Compare the worker live set against the lease rows.

        Sessions leased to this worker but not reported are orphaned:
        the turn (if any) is failed and the lease is cleared, unless a
        command for that lease is still unacked (in flight to the
        worker, which cannot have reported it yet). Sessions the worker
        reports without a matching lease come back as `lease.revoke`
        entries. Every reported session that is still leased also gets
        its effective reaper TTL, so a restarted worker learns idle
        TTLs without reading the database itself. On-disk workspaces
        the worker reports as unleased get a TTL answer while their
        session row is still alive — a released lease only means the
        Pi stopped, while the workspace intentionally stays until its
        idle TTL — and a revoke (which tells the worker to wipe them)
        only once the row is gone. The TTL answer carries the idle
        baseline (`idle_since_epoch`, the row's last touch) so the
        reaper clock does not restart on every reply.
        """
        revoke: list[dict[str, str]] = []
        ttl: dict[str, dict[str, Any]] = {}
        self.note_inventory(worker_id, reported)
        async with store.session() as db:
            rows = await list_worker_leases(db, worker_id)
            leased = {row.id: row for row in rows if row.lease_id is not None}
            for session_id, row in leased.items():
                claimed_lease = reported.get(session_id)
                if claimed_lease is not None and claimed_lease != row.lease_id:
                    revoke.append(
                        {
                            "session_id": str(session_id),
                            "lease_id": str(claimed_lease),
                        }
                    )
                    continue
                if claimed_lease is None:
                    if row.lease_id in self._unacked:
                        # The command granting this lease is still in
                        # flight: the worker cannot have reported it yet,
                        # so this is not an orphan.
                        continue
                    orphan = failure_for("worker_orphaned", "Worker lease orphaned")
                    log_event(
                        log,
                        logging.ERROR,
                        "worker lease orphaned",
                        event="worker.lease.orphaned",
                        tenant_id=row.tenant_id,
                        session_id=row.id,
                        worker_id=worker_id,
                        **log_extra(orphan),
                    )
                    await persist_event(
                        db,
                        bus,
                        row.tenant_id,
                        row.id,
                        type="agent.session.error",
                        data=session_error_data(orphan, mode="legacy"),
                    )
                    await clear_session_lease(db, row.tenant_id, row.id)
                    self._forget_delta(session_id)
                    conn = self._conns.get(worker_id)
                    if conn is not None and row.lease_id is not None:
                        conn.leases.discard(row.lease_id)
                        conn.lease_mem.pop(row.lease_id, None)
                    continue
                ttl[str(session_id)] = await self._inventory_ttl(db, row)
            for session_id, lease_id in reported.items():
                row = leased.get(session_id)
                if row is None:
                    revoke.append(
                        {
                            "session_id": str(session_id),
                            "lease_id": str(lease_id),
                        }
                    )
            for session_id in unleased:
                if session_id in reported or str(session_id) in ttl:
                    continue
                row = leased.get(session_id)
                if row is None:
                    from apipi.store.repo import get_session_by_id

                    row = await get_session_by_id(db, session_id)
                if row is None:
                    # No session row anymore (a stopped session is
                    # deleted): the directory is garbage, tell the
                    # worker to wipe it.
                    revoke.append({"session_id": str(session_id)})
                else:
                    # The row is alive: a released lease only means
                    # the Pi stopped, while the workspace stays until
                    # its idle TTL. Answer the TTL so the normal
                    # reaper wipes it when the TTL runs out.
                    ttl[str(session_id)] = await self._inventory_ttl(db, row)
        self._observe()
        return revoke, ttl

    async def _inventory_ttl(self, db: Any, row: Any) -> dict[str, Any]:
        """The effective reaper TTL answer for one session row.

        `idle_since_epoch` is the row's last touch, so the worker
        reaper measures true idleness: re-answering the TTL on every
        inventory must not restart the clock, or an idle workspace
        would never be reaped.
        """
        from apipi.worker.pi.idle import resolve_idle_ttl

        environment = row.environment if isinstance(row.environment, dict) else {}
        env_type = environment.get("type")
        agent_idle = None
        if row.agent_id is not None:
            from apipi.store.repo import get_agent

            agent = await get_agent(db, row.tenant_id, row.agent_id)
            if agent is not None:
                agent_idle = agent.idle_ttl
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        resolved = resolve_idle_ttl(
            self.settings,
            env_type if isinstance(env_type, str) else None,
            session_idle=row.idle_ttl,
            session_metadata=meta,
            agent_idle=agent_idle,
        )
        return {
            "idle_ttl_seconds": resolved.total_seconds()
            if resolved is not None
            else None,
            "env_type": env_type,
            "idle_since_epoch": _idle_since_epoch(row.updated_at),
        }

    async def resend_pending(self, conn: WorkerConnection) -> None:
        for lease_id in conn.leases:
            pending = self._unacked.get(lease_id)
            if pending is not None:
                await _send(conn.websocket, pending)

    async def handle_delta(
        self,
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
            self.observe_protocol("delta.reasoning_dropped")
            return False
        if envelope.type != "delta.text":
            return False
        try:
            payload = envelope.parsed_payload()
        except ValueError:
            self.observe_protocol("delta.invalid")
            return False
        text = getattr(payload, "text", "")
        turn_id = getattr(payload, "turn_id", None)
        if not isinstance(text, str) or not text:
            return False
        if len(text) > DELTA_MAX_TEXT:
            self.observe_protocol("delta.oversize")
            log.warning(
                "worker delta oversize",
                extra={
                    "event": "worker.delta.oversize",
                    "worker_id": str(conn.worker_id),
                    "session_id": str(envelope.session_id),
                },
            )
            return False
        if not relay_rate_allowed(
            self._delta_hits.setdefault(envelope.session_id, []),
            now=time.monotonic(),
            limit=DELTA_RATE_LIMIT,
        ):
            self.observe_protocol("delta.rate_limited")
            log.warning(
                "worker delta rate limited",
                extra={
                    "event": "worker.delta.rate_limited",
                    "worker_id": str(conn.worker_id),
                    "session_id": str(envelope.session_id),
                },
            )
            return False
        known = self._delta_leases.get(envelope.session_id)
        if (
            known is None
            or known.worker_id != conn.worker_id
            or known.lease_id not in conn.leases
            or time.monotonic() - known.refreshed > DELTA_LEASE_REFRESH
        ):
            known = await self._refresh_delta_lease(store, conn, envelope.session_id)
            if known is None:
                self._forget_delta(envelope.session_id)
                self.observe_protocol("delta.rejected")
                log.warning(
                    "worker delta for unleased session",
                    extra={
                        "event": "worker.delta.rejected",
                        "worker_id": str(conn.worker_id),
                        "session_id": str(envelope.session_id),
                    },
                )
                return False
        if turn_id is not None and str(turn_id) in known.done:
            self.observe_protocol("delta.dropped_done")
            return False
        if turn_id is not None and await self._note_done_turns(
            store, known, envelope.session_id, turn_id
        ):
            self.observe_protocol("delta.dropped_done")
            return False
        await bus.publish(
            envelope.session_id,
            live_event_body(
                envelope.session_id,
                type="agent.session.turn.output_text.delta",
                data={"delta": text, "turn_id": str(turn_id)},
            ),
        )
        self.observe_protocol("delta.accepted")
        return True

    async def _refresh_delta_lease(
        self, store: Store, conn: WorkerConnection, session_id: uuid.UUID
    ) -> _DeltaLease | None:
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
        return self._note_delta_lease(
            session_id,
            worker_id=conn.worker_id,
            lease_id=row.lease_id,
            tenant_id=row.tenant_id,
        )

    async def _note_done_turns(
        self,
        store: Store,
        known: _DeltaLease,
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

    async def handle_event(
        self,
        store: Store,
        hub: EventBus,
        *,
        lease_id: uuid.UUID,
        event_type: str,
        data: dict[str, Any] | None,
    ) -> bool:
        if event_type not in PUBLIC_EVENT_TYPES:
            return False
        async with store.session() as db:
            row = await get_session_by_lease(db, lease_id)
            if row is None:
                return False
            await persist_event(
                db,
                hub,
                row.tenant_id,
                row.id,
                type=event_type,
                data=data,
            )
        return True


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _run_mode(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _session_image(environment: dict[str, Any] | None) -> str:
    from apipi.worker.pi.sandbox import (
        image_for_size,
        sandbox_image_of,
        sandbox_size_of,
    )

    stored = sandbox_image_of(environment)
    if stored is not None:
        return stored
    return image_for_size(sandbox_size_of(environment))


def _legacy_images(arch: str | None = None) -> dict[str, WorkerImage]:
    images = {
        "default": WorkerImage("default", "legacy", "legacy", "S"),
    }
    if arch != "aarch64":
        images["browser"] = WorkerImage("browser", "legacy", "legacy", "M")
    return images


def image_unavailable_message(hub: WorkerHub, image: str) -> str:
    from apipi.worker.pi.install import recipe_archs

    arches = {
        conn.arch
        for conn in hub._conns.values()
        if "microvm" in conn.accepts and conn.arch and image not in conn.images
    }
    supported = recipe_archs(image)
    if arches and supported and arches.isdisjoint(supported):
        listed = ", ".join(sorted(arches))
        return f'sandbox_image "{image}" is not built for {listed}'
    return f'No worker has sandbox_image "{image}". Run apipi images pull on a worker.'


def images_from_message(message: dict[str, Any], kind: str) -> dict[str, WorkerImage]:
    if "images" not in message:
        if kind == "microvm":
            raw_arch = message.get("arch")
            arch = raw_arch if isinstance(raw_arch, str) else None
            return _legacy_images(arch)
        return {}
    raw = message.get("images")
    found: dict[str, WorkerImage] = {}
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, dict):
            continue
        image_id = item.get("id")
        if not isinstance(image_id, str) or not image_id:
            continue
        version = item.get("version")
        digest = item.get("digest")
        min_size = item.get("min_size")
        found[image_id] = WorkerImage(
            image_id,
            version if isinstance(version, str) else "",
            digest if isinstance(digest, str) else "",
            min_size if isinstance(min_size, str) else "S",
        )
    return found


def _payload_with_image(payload: dict[str, Any], image: str | None) -> dict[str, Any]:
    if image is None:
        return payload
    out = dict(payload)
    out["sandbox_image"] = image
    return out


def _payload_with_run_mode(
    payload: dict[str, Any] | None, required: str | None
) -> dict[str, Any]:
    out = dict(payload) if payload is not None else {}
    if required is not None:
        out["run_mode"] = required
    return out


class TokenBindingError(Exception):
    """A register presented a worker id the token is not bound to."""

    def __init__(self, worker_id: uuid.UUID) -> None:
        super().__init__(str(worker_id))
        self.worker_id = worker_id


def accepts_for_register(register: RegisterMessage) -> frozenset[str]:
    if register.accepts is not None:
        return frozenset(register.accepts)
    if register.run_mode == "microvm":
        return frozenset({"none", "microvm"})
    return frozenset({"none"})


def _store_check_for(settings: Any) -> dict[str, str] | None:
    """Issue a shared-root challenge for filesystem stores, else None."""
    if getattr(settings, "artifact_store", "local") != "local":
        return None
    from apipi.services.worker_artifacts import write_store_check
    from apipi.worker.pi.dirs import store_root

    root = store_root(settings)
    marker, nonce = write_store_check(root)
    return {"marker": marker, "nonce": nonce}


def verify_store_proof(
    settings: Any, marker: str, nonce: str, *, expected: tuple[str, str] | None
) -> bool:
    """Check a worker `store.proof` against the issued challenge."""
    if expected is None:
        return True
    if (marker, nonce) != expected:
        return False
    from apipi.services.worker_artifacts import read_store_check
    from apipi.worker.pi.dirs import store_root

    return read_store_check(store_root(settings), marker, nonce)


def answer_store_check(settings: Any, hello: dict[str, Any]) -> dict[str, str] | None:
    """Build the `store.proof` reply for a hello challenge, if any.

    Raises ConfigError with the documented shared-path message when the
    worker cannot read the marker back.
    """
    from apipi.config import ConfigError
    from apipi.services.worker_artifacts import SHARED_STORE_ERROR, read_store_check
    from apipi.worker.pi.dirs import store_root

    raw = hello.get("store_check")
    if not isinstance(raw, dict):
        return None
    marker = raw.get("marker")
    nonce = raw.get("nonce")
    if not isinstance(marker, str) or not isinstance(nonce, str):
        return None
    if not read_store_check(store_root(settings), marker, nonce):
        raise ConfigError(SHARED_STORE_ERROR)
    return {"type": "store.proof", "marker": marker, "nonce": nonce}


async def register_worker(
    hub: WorkerHub,
    store: Store,
    websocket: WebSocket,
    register: RegisterMessage,
    token: WorkerToken,
    event_hub: EventBus | None = None,
) -> WorkerConnection | None:
    run_mode = register.run_mode
    capacity = register.capacity
    memory_mb = register.memory_mb
    if memory_mb is None:
        memory_mb = capacity * hub.settings.microvm_mem_mib
    worker_id = register.id
    async with store.session() as db:
        current = await get_worker_token(db, token.id)
        if current is None or current.revoked_at is not None:
            return None
        if current.worker_id is None:
            if worker_id is None:
                worker_id = uuid.uuid4()
            await bind_worker_token(db, current, worker_id)
        elif worker_id is None:
            worker_id = current.worker_id
        elif current.worker_id != worker_id:
            raise TokenBindingError(current.worker_id)
    async with store.session() as db:
        row = await upsert_worker(
            db,
            worker_id,
            capacity=capacity,
            memory_mb=memory_mb,
            api_instance_id=hub.settings.instance_id,
        )
    conn = WorkerConnection(
        worker_id=row.id,
        generation=row.generation,
        websocket=websocket,
        capacity=row.capacity,
        memory_mb=row.memory_mb,
        run_mode=run_mode,
        token_id=token.id,
        images=images_for_register(register, run_mode),
        arch=register.arch,
        accepts=accepts_for_register(register),
    )
    await hub.attach(conn)
    sessions = await hub.restore_leases(conn, store, register.running)
    store_check = _store_check_for(hub.settings)
    if store_check is not None:
        conn.store_proof = (store_check["marker"], store_check["nonce"])
    reported = _reported_leases(register.running)
    if event_hub is not None:
        revoke, ttl = await hub.reconcile_inventory(
            store, event_hub, conn.worker_id, reported
        )
    else:
        hub.note_inventory(conn.worker_id, reported)
        revoke, ttl = [], {}
    await _send(
        websocket,
        HelloReply(
            worker_id=conn.worker_id,
            generation=conn.generation,
            sessions=sessions,
            store_check=store_check,
            revoke=[{**entry, "type": "lease.revoke"} for entry in revoke],
            ttl={uuid.UUID(key): value for key, value in ttl.items()},
        ).model_dump(mode="json"),
    )
    await hub.resend_pending(conn)
    return conn


def _reported_leases(running: list[Any] | None) -> dict[uuid.UUID, uuid.UUID]:
    """The register live set as session_id to lease_id."""
    from apipi.worker.protocol import RunningSession

    reported: dict[uuid.UUID, uuid.UUID] = {}
    for entry in running or []:
        try:
            parsed = (
                entry
                if isinstance(entry, RunningSession)
                else RunningSession.model_validate(entry)
            )
        except Exception:
            continue
        reported[parsed.session_id] = parsed.lease_id
    return reported


class CommandDedupe:
    """Remember recent command ids per session (bounded, worker-side).

    A retransmitted command (same `id`) is acked again but never
    dispatched twice, so a duplicate `turn.start` cannot start a
    second turn.
    """

    def __init__(self, limit: int = 128) -> None:
        self.limit = max(limit, 1)
        self._seen: dict[uuid.UUID, list[str]] = {}

    def duplicate(self, session_id: uuid.UUID, command_id: str) -> bool:
        known = self._seen.setdefault(session_id, [])
        if command_id in known:
            return True
        known.append(command_id)
        del known[: max(len(known) - self.limit, 0)]
        return False

    def forget(self, session_id: uuid.UUID) -> None:
        self._seen.pop(session_id, None)


def images_for_register(
    register: RegisterMessage, run_mode: str
) -> dict[str, WorkerImage]:
    accepts = accepts_for_register(register)
    if register.images is None:
        return _legacy_images(register.arch or None) if "microvm" in accepts else {}
    found: dict[str, WorkerImage] = {}
    for item in register.images:
        if not isinstance(item, dict):
            continue
        image_id = item.get("id")
        if not isinstance(image_id, str) or not image_id:
            continue
        version = item.get("version")
        digest = item.get("digest")
        min_size = item.get("min_size")
        found[image_id] = WorkerImage(
            image_id,
            version if isinstance(version, str) else "",
            digest if isinstance(digest, str) else "",
            min_size if isinstance(min_size, str) else "S",
        )
    return found


async def heartbeat_worker(
    hub: WorkerHub, store: Store, conn: WorkerConnection, message: dict[str, Any]
) -> None:
    capacity = message.get("capacity")
    if capacity is not None and _positive_int(capacity) is None:
        return
    memory_mb = message.get("memory_mb")
    if memory_mb is not None and _positive_int(memory_mb) is None:
        return
    parsed_capacity = _positive_int(capacity) if capacity is not None else None
    parsed_memory = _positive_int(memory_mb) if memory_mb is not None else None
    parsed_mode = None
    if "run_mode" in message:
        parsed_mode = _run_mode(message.get("run_mode"))
        if parsed_mode is None:
            return
    async with store.session() as db:
        await touch_worker(
            db,
            conn.worker_id,
            capacity=parsed_capacity,
            memory_mb=parsed_memory,
            api_instance_id=hub.settings.instance_id,
        )
        await extend_worker_leases(
            db,
            conn.worker_id,
            lease_until=utc_now() + hub.settings.worker_lease_ttl,
        )
    if parsed_capacity is not None:
        conn.capacity = parsed_capacity
    if parsed_memory is not None:
        conn.memory_mb = parsed_memory
    if parsed_mode is not None:
        conn.run_mode = parsed_mode
        hub._observe()
    raw_accepts = message.get("accepts")
    if isinstance(raw_accepts, list) and raw_accepts:
        cleaned = {
            str(item).strip().lower()
            for item in raw_accepts
            if isinstance(item, str)
            and str(item).strip().lower() in {"none", "microvm"}
        }
        if cleaned:
            conn.accepts = frozenset(cleaned)
            hub._observe()
    raw_arch = message.get("arch")
    if isinstance(raw_arch, str) and raw_arch:
        conn.arch = raw_arch
    if "images" in message or parsed_mode is not None:
        kind = "microvm" if "microvm" in conn.accepts else "none"
        conn.images = images_from_message(message, kind)
    if message.get("drain") is True:
        conn.draining = True
        hub._observe()
    elif message.get("drain") is False:
        conn.draining = False
        hub._observe()


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await websocket.send_json(payload)


async def _close(websocket: WebSocket, reason: str | None = None) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        if reason is not None:
            await websocket.close(code=1008, reason=reason)
        else:
            await websocket.close()


def _sink_for_execution(
    execution: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Any | None:
    sink_for = getattr(execution, "sink_for", None)
    if not callable(sink_for):
        return None
    return sink_for(tenant_id, session_id)


async def _reject_missing_image(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    image: str,
    request_id: str | None,
) -> None:
    store = getattr(execution, "store", None)
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    message = (
        image_unavailable_message(hub, image)
        if hub is not None
        else (
            f'No worker has sandbox_image "{image}". Run apipi images pull on a worker.'
        )
    )
    log_event(
        log,
        logging.WARNING,
        "worker image missing",
        event="worker.placement.rejected",
        error_code="image_unavailable",
        tenant_id=tenant_id,
        session_id=session_id,
        request_id=request_id,
    )
    from apipi.services.sink import resolve_sink

    active = resolve_sink(_sink_for_execution(execution, tenant_id, session_id))
    async with active.txn(store) as db:
        await active.append_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.error",
            data={"message": message, "code": "image_unavailable"},
        )
        await active.append_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.failed",
            data={"message": message},
        )


async def _reject_mismatched_turn(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    required: str,
    worker_mode: str,
    request_id: str | None,
) -> None:
    log_event(
        log,
        logging.WARNING,
        "worker placement rejected",
        event="worker.placement.rejected",
        error_code="placement",
        tenant_id=tenant_id,
        session_id=session_id,
        request_id=request_id,
        run_mode=worker_mode,
    )
    store = getattr(execution, "store", None)
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    message = f"Worker does not accept {required} sessions"
    from apipi.services.sink import resolve_sink

    active = resolve_sink(_sink_for_execution(execution, tenant_id, session_id))
    async with active.txn(store) as db:
        await active.append_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.error",
            data=session_error_data(
                failure_for("placement", message),
                mode="legacy",
            ),
        )
        placed = failure_for("placement", message)
        failed = turn_failed_data("", placed)
        failed.pop("turn_id", None)
        await active.append_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.failed",
            data=failed,
        )


def _check_command_context(op: str, payload: dict[str, Any]) -> None:
    """Validate the turn context on worker commands before sending."""
    raw = payload.get("context")
    if raw is None:
        return
    try:
        parse_turn_context(raw)
    except (ContextBytes, ValidationError) as exc:
        raise ApiError(
            "invalid_request",
            f"invalid turn context: {exc}",
            code="invalid_request",
            status_code=400,
        ) from exc
    try:
        check_command_size(payload)
    except CommandTooLarge as exc:
        raise ApiError(
            "invalid_request",
            str(exc),
            code="payload_too_large",
            status_code=413,
        ) from exc


_TURN_OPS = frozenset({"turn.start", "turn.continue"})


def _command_turn_context(op: object, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the command context on the worker; invalid fails the turn."""
    if op not in COMMAND_CONTEXT_OPS:
        return None
    raw = payload.get("context")
    if raw is None:
        return None
    try:
        return parse_turn_context(raw).model_dump()
    except (ContextBytes, ValidationError) as exc:
        raise ApiError(
            "invalid_request",
            f"invalid turn context: {exc}",
            code="invalid_request",
            status_code=400,
        ) from exc


def _command_log_context(message: dict[str, Any]) -> dict[str, Any]:
    """Secret-free context summary for the worker command log."""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("context")
    if not isinstance(raw, dict):
        return {}
    return {"context": summarize_context(raw)}


async def _report_escaped_turn(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    exc: BaseException,
) -> None:
    store = getattr(execution, "store", None)
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    from apipi.services.sink import resolve_sink

    active = resolve_sink(_sink_for_execution(execution, tenant_id, session_id))
    async with active.txn(store) as db:
        row = await get_session(db, tenant_id, session_id) if db is not None else None
        if row is not None and row.status == "failed":
            return
        if isinstance(exc, ApiError):
            message = exc.message
            code = exc.code or "internal"
        else:
            message = "Turn failed"
            code = "internal"
        await fail_session(
            db, hub, tenant_id, session_id, message, code=code, sink=active
        )


async def dispatch_command(execution: Any, message: dict[str, Any]) -> None:
    op = message.get("op")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    try:
        session_id = uuid.UUID(str(message.get("session_id")))
        tenant_id = uuid.UUID(str(payload.get("tenant_id")))
    except (ValueError, TypeError):
        return
    request_id = payload.get("request_id")
    api_key = payload.get("api_key")
    key_id = payload.get("key_id")
    user_id = payload.get("user_id")
    org_id = payload.get("org_id")
    request_id = request_id if isinstance(request_id, str) else None
    api_key = api_key if isinstance(api_key, str) else None
    key_id = key_id if isinstance(key_id, str) else None
    user_id = user_id if isinstance(user_id, str) else None
    org_id = org_id if isinstance(org_id, str) else None
    raw_parent = payload.get("traceparent")
    token = attach_traceparent(raw_parent if isinstance(raw_parent, str) else None)
    try:
        turn_context = _command_turn_context(op, payload)
        await _run_command(
            execution,
            op,
            tenant_id,
            session_id,
            payload,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
        )
    except ApiError as exc:
        command_failure = failure_for(exc.code or "internal", exc.message)
        log_event(
            log,
            log_level_for_code(command_failure.code, command_failure.failure_source),
            "worker command failed",
            event="worker.command.failed",
            exc_info=exc,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            **log_extra(command_failure),
        )
        if op in _TURN_OPS:
            await _report_escaped_turn(execution, tenant_id, session_id, exc)
            return
        raise
    except Exception as exc:
        internal = failure_for("internal", "Turn failed")
        log_event(
            log,
            logging.ERROR,
            "worker command failed",
            event="worker.command.failed",
            exc_info=exc,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            **log_extra(internal),
        )
        if op in _TURN_OPS:
            await _report_escaped_turn(execution, tenant_id, session_id, exc)
            return
        raise
    finally:
        detach_traceparent(token)


async def _run_command(
    execution: Any,
    op: object,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    request_id: str | None,
    api_key: str | None,
    key_id: str | None,
    user_id: str | None,
    org_id: str | None = None,
    turn_context: dict[str, Any] | None = None,
) -> None:
    if op == "turn.start":
        required = payload.get("run_mode")
        settings = getattr(execution, "settings", None)
        if isinstance(required, str) and settings is not None:
            from apipi.worker.accepts import resolved_worker_accepts

            accepts = resolved_worker_accepts(settings)
            if not worker_accepts(accepts, required):
                await _reject_mismatched_turn(
                    execution,
                    tenant_id,
                    session_id,
                    required=required,
                    worker_mode=",".join(sorted(accepts)),
                    request_id=request_id,
                )
                return
        wanted = payload.get("sandbox_image")
        worker_mode = (
            getattr(settings, "run_mode", None) if settings is not None else None
        )
        if (
            isinstance(wanted, str)
            and worker_mode == "microvm"
            and isinstance(getattr(execution, "settings", None), object)
        ):
            from apipi.worker.pi.image_pull import available_images

            have = {item.id for item in available_images(execution.settings)}
            if wanted not in have:
                await _reject_missing_image(
                    execution,
                    tenant_id,
                    session_id,
                    image=wanted,
                    request_id=request_id,
                )
                return
        text = payload.get("text")
        raw_images = payload.get("images")
        images = raw_images if isinstance(raw_images, list) else None
        raw_parts = payload.get("parts")
        parts = raw_parts if isinstance(raw_parts, list) else None
        await execution.run_turn(
            tenant_id,
            session_id,
            text if isinstance(text, str) else "",
            images=images,
            parts=parts,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return
    if op == "turn.continue":
        raw_turn = payload.get("turn_id")
        call_id = payload.get("call_id")
        success = payload.get("success")
        if not isinstance(raw_turn, str) or not isinstance(call_id, str):
            return
        if not isinstance(success, bool):
            return
        output = payload.get("output")
        error = payload.get("error")
        await execution.continue_turn(
            tenant_id,
            session_id,
            turn_id=uuid.UUID(raw_turn),
            call_id=call_id,
            success=success,
            output=output if isinstance(output, str) else None,
            error=error if isinstance(error, str) else None,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return
    if op == "turn.cancel":
        await execution.cancel(session_id, status="in_progress")
        return
    if op == "session.stop":
        await execution.teardown(session_id)
        await _wipe_stopped_session(execution, tenant_id, session_id)
        return
    if op == "sandbox.boot":
        await execution.boot_hosted(tenant_id, session_id, turn_context=turn_context)


async def _wipe_stopped_session(
    execution: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    from apipi.worker.pi.artifacts import wipe_workspace

    settings = getattr(execution, "settings", None)
    outbox = getattr(execution, "outbox", None)
    if outbox is not None and settings is not None:
        # Split mode wipes without the database: the local workspace
        # directory follows the shared sessions-root layout, the blob
        # delete happens on the API when it ingests `session.stopped`,
        # and the envelope is the durable receipt.
        base = getattr(settings, "sessions_dir", "")
        root = Path(base) if base else Path.cwd() / ".apipi" / "sessions"
        wipe_workspace(root / str(tenant_id) / str(session_id))
        try:
            outbox.append(session_id, "session.stopped", {"reason": "stop"})
        except Exception:
            log.exception(
                "session stopped report failed",
                extra={"session_id": str(session_id)},
            )
        return
    store = getattr(execution, "store", None)
    if store is None or settings is None:
        return
    from apipi.store.blobs import blob_store
    from apipi.worker.pi.artifacts import wipe_artifact_store, wipe_workspace

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
    if row is None:
        return
    environment = row.environment if isinstance(row.environment, dict) else {}
    directory = environment.get("directory")
    if isinstance(directory, str) and directory:
        wipe_workspace(Path(directory))
    await wipe_artifact_store(blob_store(settings), tenant_id, row.key_id, session_id)


def worker_ws_url(base: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme in {"http", "https"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        parsed = parsed._replace(scheme=scheme)
    elif parsed.scheme not in {"ws", "wss"}:
        raise ConfigError("APIPI_API_URL must be an http URL")
    path = parsed.path.rstrip("/") + "/internal/worker"
    return urlunparse(parsed._replace(path=path, fragment=""))


def worker_arch() -> str:
    return os.uname().machine


def _worker_connect_kwargs(settings: Settings, ws_url: str) -> dict[str, Any]:
    """Extra kwargs for the worker's `websockets.connect` call.

    `ssl` is only passed when there is a real context: websockets
    raises for `ssl=None` on a `wss://` URI, and omitting it already
    verifies against the system trust store. Plain `ws://`
    (loopback only) needs no context."""
    from apipi.worker.tls import worker_ssl_context

    context = worker_ssl_context(settings) if ws_url.startswith("wss") else None
    return {"ssl": context} if context is not None else {}


def _heartbeat_images(settings: Settings) -> list[dict[str, str]]:
    from apipi.worker.accepts import resolved_worker_accepts

    if "microvm" not in resolved_worker_accepts(settings):
        return []
    from apipi.worker.pi.image_pull import available_images

    return [
        {
            "id": item.id,
            "version": item.version,
            "digest": item.digest,
            "min_size": item.min_size,
        }
        for item in available_images(settings)
    ]


def worker_heartbeat(settings: Settings, *, drain: bool = False) -> dict[str, object]:
    from apipi.worker.accepts import resolved_worker_accepts

    payload: dict[str, object] = {
        "type": "heartbeat",
        "capacity": settings.max_sessions,
        "memory_mb": settings.node_memory_mb(),
        "run_mode": settings.run_mode,
        "accepts": sorted(resolved_worker_accepts(settings)),
        "arch": worker_arch(),
        "image_store_version": settings.image_store_version or "",
        "images": _heartbeat_images(settings),
    }
    if drain:
        payload["drain"] = True
    return payload


def drain_idle(live: int, command_tasks: set[asyncio.Task[None]]) -> bool:
    return live == 0 and not command_tasks


def drain_timeout_seconds(settings: Settings, drain_timeout: float | None) -> float:
    if drain_timeout is not None:
        return drain_timeout
    return settings.idle_ttl.total_seconds()


def _install_drain_signals(draining: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_drain() -> None:
        draining.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, request_drain)


async def run_worker(
    settings: Settings,
    *,
    url: str | None = None,
    drain_timeout: float | None = None,
) -> int:
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.lifecycle_export import (
        OutboxLifecycleReporter,
        worker_lifecycle_ignored,
    )
    from apipi.worker.accepts import require_worker_accepts
    from apipi.worker.execution import local_execution, worker_observability
    from apipi.worker.tls import check_worker_mtls_files, require_worker_tls

    reject_legacy_worker_token()
    require_worker_accepts(settings)
    ignored_lifecycle = worker_lifecycle_ignored()
    if ignored_lifecycle:
        log.warning(
            "worker ignores API-only lifecycle settings",
            extra={
                "event": "worker.lifecycle.ignored",
                "settings": ignored_lifecycle,
            },
        )
    token = load_worker_token(settings.worker_token_file)
    base = url or settings.api_url or "http://127.0.0.1:8000"
    ws_url = require_worker_tls(base)
    check_worker_mtls_files(settings)
    heartbeat = min(10.0, max(1.0, settings.worker_lease_ttl.total_seconds() / 2))
    metrics, tracing = worker_observability(settings)
    # A split worker holds no database and no object-store credentials.
    # Turn context arrives in commands, live deltas go over the socket
    # through the relay, and durable results go through the outbox
    # with a cumulative ack, so the event bus is always in memory.
    bus = InMemoryEventBus()
    relay = DeltaRelay()
    outbox = worker_outbox(settings)
    execution = local_execution(
        settings,
        store=None,
        hub=LiveRedirectBus(bus, relay),
        metrics=metrics,
        tracing=tracing,
        outbox=outbox,
    )
    # Split workers report lifecycle over the socket; the API owns the
    # export. The reporter only appends durable envelopes, and the
    # reaper learns idle TTLs from inventory replies, so no background
    # loop needs the database.
    execution.pool.lifecycle = OutboxLifecycleReporter(outbox)
    execution.db_fallback = False
    await bus.start()
    tasks: set[asyncio.Task[None]] = set()
    if metrics is not None:
        from apipi.worker.scrape import serve_metrics

        tasks.add(
            asyncio.create_task(
                serve_metrics(
                    metrics,
                    host=settings.worker_metrics_host,
                    port=settings.worker_metrics_port,
                )
            )
        )
        log.info(
            "worker metrics",
            extra={
                "host": settings.worker_metrics_host,
                "port": settings.worker_metrics_port,
            },
        )
    tasks.add(asyncio.create_task(execution.observe_loop()))
    tasks.add(asyncio.create_task(execution.reap_loop()))
    tasks.add(asyncio.create_task(execution.reap_workspace_loop()))
    lifecycle = getattr(execution, "lifecycle_loop", None)
    if lifecycle is not None:
        tasks.add(asyncio.create_task(lifecycle()))
    seen = getattr(execution, "sandbox_seen_loop", None)
    if seen is not None:
        tasks.add(asyncio.create_task(seen()))
    emitter = getattr(getattr(execution, "pool", None), "lifecycle", None)
    if emitter is not None:
        emitter.start()
    log.info("worker connect", extra={"url": ws_url})
    spooled = outbox.load_spool()
    if spooled:
        log.info(
            "worker outbox spool loaded",
            extra={"sessions": len(spooled)},
        )
    draining = asyncio.Event()
    _install_drain_signals(draining)
    drain_deadline: float | None = None
    wait = drain_timeout_seconds(settings, drain_timeout)
    command_tasks: set[asyncio.Task[None]] = set()
    session_leases: dict[uuid.UUID, str] = {}
    # Command dedupe is worker-lifetime, not per-connection: the API
    # replays unacked commands after a reconnect with the same
    # `command_id`, and only a dedupe that survives the socket tells
    # the replay from a new turn. Entries are forgotten when the
    # session is torn down, stopped, or revoked.
    dedupe = CommandDedupe()
    status = 0
    backoff = 0.5
    connect_kwargs = _worker_connect_kwargs(settings, ws_url)
    try:
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    **connect_kwargs,
                ) as sock:
                    outcome, drain_deadline = await _serve_connection(
                        settings,
                        execution,
                        outbox,
                        relay,
                        sock,
                        session_leases,
                        command_tasks,
                        tasks,
                        draining,
                        drain_deadline,
                        wait,
                        heartbeat,
                        emitter,
                        dedupe,
                    )
                backoff = 0.5
                if outcome == "drained":
                    break
                if outcome == "drain_timeout":
                    status = 1
                    break
            except ConfigError:
                raise
            except Exception:
                log.warning("worker connection lost, reconnecting", exc_info=True)
                if draining.is_set() and drain_idle(
                    execution.pool.live(), command_tasks
                ):
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)
                continue
    finally:
        relay.detach()
        for task in tasks:
            task.cancel()
        await execution.close()
        if execution.tracing is not None:
            execution.tracing.shutdown()
        await bus.close()
    return status


def worker_outbox(settings: Settings) -> "Outbox":
    """Build the worker outbox from settings."""
    from apipi.worker.outbox import Outbox

    return Outbox(
        max_messages=settings.worker_outbox_max_messages,
        max_bytes=settings.worker_outbox_max_bytes,
        spool_dir=settings.worker_outbox_dir,
    )


async def _pump_outbox(outbox: "Outbox", send: Any) -> None:
    """Send buffered durable envelopes until cancelled (one writer)."""
    while True:
        await outbox.wait_dirty()
        for session_id in outbox.pending_sessions():
            for envelope in outbox.pending(session_id):
                await send(envelope)


def _running_claim(
    session_leases: dict[uuid.UUID, str], outbox: "Outbox"
) -> list[dict[str, Any]]:
    claimed = []
    for session_id, lease_id in session_leases.items():
        claimed.append(
            {
                "session_id": str(session_id),
                "lease_id": lease_id,
                "last_seq": outbox.high_water(session_id),
            }
        )
    return claimed


async def _reconcile_hello(
    execution: Any,
    outbox: "Outbox",
    relay: Any,
    session_leases: dict[uuid.UUID, str],
    sessions: dict[str, Any],
    hello: dict[str, Any] | None = None,
    dedupe: "CommandDedupe | None" = None,
    settings: Any | None = None,
) -> None:
    """Adopt the API cursors; drop turns the API no longer leases."""
    kept: set[uuid.UUID] = set()
    for raw_id, last_seq in sessions.items():
        try:
            session_id = uuid.UUID(str(raw_id))
            cursor = int(last_seq)
        except (ValueError, TypeError):
            continue
        kept.add(session_id)
        outbox.set_base(session_id, max(cursor, 0))
    for session_id in [sid for sid in session_leases if sid not in kept]:
        session_leases.pop(session_id, None)
        relay.forget(session_id)
        if dedupe is not None:
            dedupe.forget(session_id)
        await execution.teardown(session_id)
        outbox.drop_session(session_id)
        log.info(
            "worker dropped unleased session", extra={"session_id": str(session_id)}
        )
    for session_id in outbox.pending_sessions():
        if session_id not in kept and session_id not in session_leases:
            outbox.drop_session(session_id)
    if hello is not None:
        await _apply_inventory_reply(
            execution,
            hello,
            session_leases=session_leases,
            outbox=outbox,
            relay=relay,
            dedupe=dedupe,
            settings=settings,
        )
    outbox.mark_dirty()


def _idle_since_epoch(updated_at: Any) -> float | None:
    """Unix baseline for the reaper idle clock (stable across replies)."""
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.timestamp()


def _seed_reaper_ttl(execution: Any, ttl: Any) -> None:
    """Teach the reaper idle TTLs from an inventory reply (no DB read)."""
    remember = getattr(execution, "_context_ttl", None)
    if not isinstance(remember, dict) or not isinstance(ttl, dict):
        return
    now = time.time()
    for raw_id, entry in ttl.items():
        try:
            session_id = str(uuid.UUID(str(raw_id)))
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        raw_seconds = entry.get("idle_ttl_seconds")
        seconds = (
            float(raw_seconds)
            if isinstance(raw_seconds, (int, float)) and raw_seconds >= 0
            else None
        )
        raw_since = entry.get("idle_since_epoch")
        if isinstance(raw_since, (int, float)) and 0 < raw_since <= now:
            last_seen = float(raw_since)
        else:
            last_seen = now
        known = remember.get(session_id)
        if known is not None:
            # Never move the reaper clock backwards: the worker's own
            # turn activity (`refresh_context_seen`) is newer than the
            # row touch whenever the row is not updated per turn.
            last_seen = max(last_seen, known[1])
        env_type = entry.get("env_type")
        remember[session_id] = (
            seconds,
            last_seen,
            env_type if isinstance(env_type, str) else None,
        )


async def _apply_inventory_reply(
    execution: Any,
    reply: dict[str, Any],
    *,
    session_leases: dict[uuid.UUID, str] | None = None,
    outbox: Any | None = None,
    relay: Any | None = None,
    dedupe: "CommandDedupe | None" = None,
    settings: Any | None = None,
) -> None:
    """Apply revocations and reaper TTLs from an inventory reply."""
    _seed_reaper_ttl(execution, reply.get("ttl"))
    revoke = reply.get("revoke")
    if not isinstance(revoke, list):
        return
    for entry in revoke:
        if not isinstance(entry, dict):
            continue
        raw_session = entry.get("session_id")
        try:
            session_id = uuid.UUID(str(raw_session))
        except (ValueError, TypeError):
            continue
        if dedupe is not None:
            dedupe.forget(session_id)
        known = session_leases is not None and session_id in session_leases
        if session_leases is not None:
            session_leases.pop(session_id, None)
        if relay is not None:
            forget = getattr(relay, "forget", None)
            if callable(forget):
                forget(session_id)
        await execution.teardown(session_id)
        if outbox is not None:
            drop = getattr(outbox, "drop_session", None)
            if callable(drop):
                drop(session_id)
        if settings is not None and not known:
            # Revoked without a local lease: an on-disk workspace the
            # worker only reported as unleased. Wipe it so reaped
            # leftovers do not accumulate after a restart.
            await _wipe_unknown_workspace(settings, execution, outbox, session_id)
        log.info("worker revoked session", extra={"session_id": str(session_id)})


async def _wipe_unknown_workspace(
    settings: Any, execution: Any, outbox: Any | None, session_id: uuid.UUID
) -> None:
    """Wipe an on-disk workspace the worker holds no lease for.

    Runs after teardown for revokes of sessions absent from the local
    lease set (typically unleased dirs reported in the inventory). The
    pool liveness guard keeps a racing fresh turn safe, and the receipt
    lets the API delete the session blobs.
    """
    pool = getattr(execution, "pool", None)
    try:
        if pool is not None and (pool.alive(session_id) or pool.held(session_id)):
            return
    except Exception:
        return
    from apipi.worker.pi.artifacts import wipe_workspace
    from apipi.worker.pi.dirs import sessions_root

    try:
        root = sessions_root(settings)
    except Exception:
        return
    try:
        tenants = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return
    wiped = False
    for tenant_dir in tenants:
        if tenant_dir.name.startswith("."):
            continue
        try:
            uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        workspace = tenant_dir / str(session_id)
        try:
            if not workspace.is_dir():
                continue
        except OSError:
            continue
        try:
            wipe_workspace(workspace)
        except Exception:
            log.exception(
                "unknown workspace wipe failed",
                extra={"session_id": str(session_id)},
            )
            continue
        wiped = True
    if wiped and outbox is not None:
        append = getattr(outbox, "append", None)
        if callable(append):
            try:
                append(session_id, "workspace.reaped", {"reason": "revoked"})
            except Exception:
                log.exception(
                    "revoked workspace report failed",
                    extra={"session_id": str(session_id)},
                )


def _unleased_session_dirs(
    settings: Any,
    session_leases: dict[uuid.UUID, str],
    pool: Any | None,
) -> list[uuid.UUID]:
    """On-disk session workspaces the worker holds no lease for.

    These ride along in the periodic inventory so the API answers
    with a reaper TTL while the session row is alive and a revoke
    (which wipes them) only once the row is gone. Live or held
    guests are never reported.
    """
    from apipi.worker.pi.dirs import sessions_root

    try:
        root = sessions_root(settings)
    except Exception:
        return []
    try:
        tenants = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []
    found: list[uuid.UUID] = []
    for tenant_dir in tenants:
        if tenant_dir.name.startswith("."):
            continue
        try:
            uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        try:
            sessions = [entry for entry in tenant_dir.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for session_dir in sessions:
            try:
                session_id = uuid.UUID(session_dir.name)
            except ValueError:
                continue
            if session_id in session_leases or session_id in found:
                continue
            if pool is not None:
                try:
                    if pool.alive(session_id) or pool.held(session_id):
                        continue
                except Exception:
                    continue
            found.append(session_id)
    return found


async def _serve_connection(
    settings: Settings,
    execution: Any,
    outbox: "Outbox",
    relay: Any,
    sock: Any,
    session_leases: dict[uuid.UUID, str],
    command_tasks: set[asyncio.Task[None]],
    tasks: set[asyncio.Task[None]],
    draining: asyncio.Event,
    drain_deadline: float | None,
    wait: float,
    heartbeat: float,
    emitter: Any,
    dedupe: CommandDedupe,
) -> tuple[str, float | None]:
    """Serve one socket; returns drained or drain_timeout (loss raises).

    `dedupe` is worker-lifetime (owned by `run_worker`): reconnects
    replay unacked commands with the same `command_id`, so only a
    dedupe that survives the socket suppresses the second dispatch.
    """
    from apipi.worker.accepts import resolved_worker_accepts
    from apipi.worker.protocol import PROTOCOL_VERSION

    send_lock = asyncio.Lock()

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await sock.send(json.dumps(payload))

    await send_json(
        {
            "type": "register",
            "protocol": PROTOCOL_VERSION,
            "capabilities": {},
            "accepts": sorted(resolved_worker_accepts(settings)),
            "running": _running_claim(session_leases, outbox),
            "capacity": settings.max_sessions,
            "memory_mb": settings.node_memory_mb(),
            "run_mode": settings.run_mode,
            "arch": worker_arch(),
            "images": _heartbeat_images(settings),
        }
    )
    raw = await sock.recv()
    hello = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
    if not isinstance(hello, dict) or not hello.get("ok"):
        error = hello.get("error") if isinstance(hello, dict) else "unauthorized"
        raise ConfigError(f"worker register failed: {error}")
    proof = answer_store_check(settings, hello if isinstance(hello, dict) else {})
    if proof is not None:
        await send_json(proof)
    sessions = hello.get("sessions")
    hello_sessions = sessions if isinstance(sessions, dict) else {}
    log.info(
        "worker hello",
        extra={
            "worker_id": hello.get("worker_id"),
            "sessions": hello_sessions,
        },
    )

    async def release_lease(session_id: uuid.UUID) -> None:
        relay.forget(session_id)
        lease_id = session_leases.pop(session_id, None)
        if lease_id is None:
            return
        try:
            await send_json(
                {
                    "type": "lease.release",
                    "session_id": str(session_id),
                    "lease_id": lease_id,
                }
            )
        except Exception:
            log.exception("lease release failed")

    execution.note_stopped = release_lease
    raw_worker = hello.get("worker_id")
    if emitter is not None:
        emitter.set_worker_id(str(raw_worker) if raw_worker else None)
    await _reconcile_hello(
        execution,
        outbox,
        relay,
        session_leases,
        hello_sessions,
        hello,
        dedupe=dedupe,
        settings=settings,
    )
    relay.attach(send_json)
    pump = asyncio.create_task(_pump_outbox(outbox, send_json))

    async def report_seen(session_ids: list[uuid.UUID]) -> None:
        if getattr(execution, "seen_hook", None) is not report_seen:
            return
        await send_json(
            {
                "type": "sandbox.seen",
                "session_ids": [str(session_id) for session_id in session_ids],
            }
        )

    execution.seen_hook = report_seen

    async def send_inventory() -> None:
        unleased = _unleased_session_dirs(
            settings, session_leases, getattr(execution, "pool", None)
        )
        await send_json(
            {
                "type": "inventory",
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "lease_id": lease_id,
                        "last_seq": outbox.high_water(session_id),
                    }
                    for session_id, lease_id in session_leases.items()
                ]
                + [
                    {"session_id": str(session_id), "last_seq": 0}
                    for session_id in unleased
                ],
            }
        )

    last_inventory = time.monotonic()

    async def send_heartbeat() -> None:
        await send_json(worker_heartbeat(settings, drain=draining.is_set()))
        nonlocal last_inventory
        if time.monotonic() - last_inventory >= 60.0:
            await send_inventory()
            last_inventory = time.monotonic()

    try:
        while True:
            if draining.is_set() and drain_deadline is None:
                drain_deadline = time.monotonic() + wait
                log.info("worker drain")
                await send_heartbeat()
                await execution.pool.kill_unheld(reason="drain")
                if drain_idle(execution.pool.live(), command_tasks):
                    return "drained", drain_deadline
            recv_timeout = 0.5 if draining.is_set() else heartbeat
            try:
                incoming = await asyncio.wait_for(sock.recv(), timeout=recv_timeout)
            except TimeoutError:
                await send_heartbeat()
                if draining.is_set():
                    await execution.pool.kill_unheld(reason="drain")
                    if drain_idle(execution.pool.live(), command_tasks):
                        return "drained", drain_deadline
                    if (
                        drain_deadline is not None
                        and time.monotonic() >= drain_deadline
                    ):
                        return "drain_timeout", drain_deadline
                continue
            text = incoming if isinstance(incoming, str) else incoming.decode()
            message = json.loads(text)
            if not isinstance(message, dict):
                continue
            if message.get("type") == "ack":
                raw_session = message.get("session_id")
                last_seq = message.get("last_seq")
                try:
                    ack_session = uuid.UUID(str(raw_session))
                    ack_seq = int(last_seq)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    continue
                outbox.acked(ack_session, ack_seq)
                continue
            if message.get("type") == "artifact.presign.reply":
                from apipi.worker.artifact_upload import handle_presign_reply

                waiters = getattr(execution, "presign_waiters", None)
                if isinstance(waiters, dict):
                    handle_presign_reply(waiters, message)
                continue
            if message.get("type") == "inventory.reply":
                await _apply_inventory_reply(
                    execution,
                    message,
                    session_leases=session_leases,
                    outbox=outbox,
                    relay=relay,
                    dedupe=dedupe,
                    settings=settings,
                )
                continue
            if message.get("type") == "command":
                raw_lease = message.get("lease_id")
                raw_session = message.get("session_id")
                if isinstance(raw_lease, str) and isinstance(raw_session, str):
                    session_leases[uuid.UUID(raw_session)] = raw_lease
            if message.get("type") == "command" and message.get("op") == "session.stop":
                command_id = message.get("id")
                try:
                    stop_session = uuid.UUID(str(message.get("session_id")))
                except (ValueError, TypeError):
                    stop_session = None
                if (
                    stop_session is not None
                    and isinstance(command_id, str)
                    and dedupe.duplicate(stop_session, command_id)
                ):
                    await send_json(
                        {
                            "type": "lease.ack",
                            "id": message.get("id"),
                            "lease_id": message.get("lease_id"),
                        }
                    )
                    continue
                await dispatch_command(execution, message)
                if stop_session is not None:
                    dedupe.forget(stop_session)
                await send_json(
                    {
                        "type": "lease.ack",
                        "id": message.get("id"),
                        "lease_id": message.get("lease_id"),
                    }
                )
                continue
            if message.get("type") == "lease.revoke":
                revoked = message.get("session_id")
                if isinstance(revoked, str):
                    try:
                        revoked_id = uuid.UUID(revoked)
                    except ValueError:
                        continue
                    dedupe.forget(revoked_id)
                    known = revoked_id in session_leases
                    session_leases.pop(revoked_id, None)
                    relay.forget(revoked_id)
                    await execution.teardown(revoked_id)
                    outbox.drop_session(revoked_id)
                    if not known:
                        await _wipe_unknown_workspace(
                            settings, execution, outbox, revoked_id
                        )
                continue
            if message.get("type") == "command":
                command_id = message.get("id")
                try:
                    command_session = uuid.UUID(str(message.get("session_id")))
                except (ValueError, TypeError):
                    command_session = None
                if (
                    command_session is not None
                    and isinstance(command_id, str)
                    and dedupe.duplicate(command_session, command_id)
                ):
                    await send_json(
                        {
                            "type": "lease.ack",
                            "id": message.get("id"),
                            "lease_id": message.get("lease_id"),
                        }
                    )
                    continue
                await send_json(
                    {
                        "type": "lease.ack",
                        "id": message.get("id"),
                        "lease_id": message.get("lease_id"),
                    }
                )
                log.info(
                    "worker command",
                    extra={
                        "op": message.get("op"),
                        "session_id": message.get("session_id"),
                        **_command_log_context(message),
                    },
                )
                task = asyncio.create_task(dispatch_command(execution, message))
                command_tasks.add(task)
                task.add_done_callback(command_tasks.discard)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        if getattr(execution, "seen_hook", None) is report_seen:
            execution.seen_hook = None
        relay.detach()
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
