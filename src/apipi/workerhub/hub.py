import asyncio
import logging
import time
import uuid
from collections.abc import Collection, Sequence
from datetime import UTC
from typing import Any

from apipi.common.errors import ApiError
from apipi.common.event_bus import EventBus
from apipi.common.failures import (
    failure_for,
    log_extra,
    session_error_data,
)
from apipi.common.logutil import RateLimitedLog, log_event
from apipi.common.otel import (
    Tracing,
    start_span,
)
from apipi.common.placement import placement_for
from apipi.common.sandbox import mem_mib_for_size, sandbox_size_of
from apipi.config import (
    Settings,
)
from apipi.protocol import (
    BASELINE_FEATURES,
    COMMAND_OPS,
    CURSOR_OPS,
    OP_FEATURES,
    PUBLIC_EVENT_TYPES,
    BaseCommandPayload,
    LeaseRevoke,
    RunningSession,
    WorkerCommand,
    WorkerEnvelope,
)
from apipi.services.session_events import persist_event
from apipi.services.turn_state import fail_stale_in_progress
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    clear_session_lease,
    extend_worker_leases,
    get_session,
    get_session_by_lease,
    list_expired_leases,
    list_worker_leases,
    renew_session_leases,
    set_session_lease,
)
from apipi.workerhub import deltas as delta_gate
from apipi.workerhub import inventory as inventory_gate
from apipi.workerhub.command_queue import (
    CommandQueue,
    CommandQueueFull,
    PendingCommand,
)
from apipi.workerhub.commands import (
    _check_command_context,
    command_payload,
    image_unavailable_message,
    session_image,
)
from apipi.workerhub.connection import WorkerConnection, claimed_leases
from apipi.workerhub.deltas import DeltaLease

log = logging.getLogger("apipi.worker")

REVOKE_TIMEOUT = 5.0
COMMAND_RETRANSMIT_SECONDS = 5.0
MAX_HEARTBEAT_SECONDS = 10.0
MIN_HEARTBEAT_SECONDS = 0.05


def _request_id(payload: BaseCommandPayload | dict[str, Any] | None) -> Any:
    if isinstance(payload, BaseCommandPayload):
        return payload.request_id
    return payload.get("request_id") if isinstance(payload, dict) else None


def heartbeat_interval(settings: Settings) -> float:
    """The heartbeat interval the API tells every worker: a third of the TTL."""
    ttl = settings.worker_lease_ttl.total_seconds()
    return min(MAX_HEARTBEAT_SECONDS, max(ttl / 3, MIN_HEARTBEAT_SECONDS))


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
        self.commands = CommandQueue()
        self._stopped: dict[uuid.UUID, asyncio.Event] = {}
        self.retransmit_seconds = COMMAND_RETRANSMIT_SECONDS
        self._warnings = RateLimitedLog(log)
        self._delta_hits: dict[uuid.UUID, list[float]] = {}
        self._delta_leases: dict[uuid.UUID, DeltaLease] = {}
        self._inventory: dict[uuid.UUID, dict[uuid.UUID, uuid.UUID]] = {}
        self._metric_modes: set[str] = set()
        self._lock = asyncio.Lock()

    def live(self) -> int:
        return len(self._conns)

    def observe_protocol(self, event: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_protocol(event)

    def observe_connect(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_connect(result)

    def observe_disconnect(self, reason: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_disconnect(reason)

    async def _send(self, conn: WorkerConnection, wire: dict[str, Any]) -> None:
        if conn.writer.metrics is None:
            conn.writer.bind(self.metrics, self.observe_send_queue)
        await conn.send(wire)

    def observe_send_queue(self) -> None:
        if self.metrics is not None:
            self.metrics.set_worker_send_queue_depth(
                sum(conn.writer.depth for conn in self._conns.values())
            )

    def observe_lease_event(self, event: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_lease_event(event)

    def observe_command(self, op: str, result: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_command(op, result)

    def _enqueue(self, wire: dict[str, Any], conn: WorkerConnection) -> PendingCommand:
        feature = OP_FEATURES.get(str(wire.get("op")))
        if feature is not None and feature not in conn.features:
            raise ApiError(
                "api_error",
                f"The worker does not support {wire.get('op')}",
                code="unsupported_op",
                status_code=501,
            )
        try:
            entry = self.commands.push(wire, worker_id=conn.worker_id)
        except CommandQueueFull:
            raise ApiError(
                "invalid_request",
                "The worker has too many unacked commands for this session",
                code="capacity",
                status_code=429,
            ) from None
        self._observe_unacked()
        return entry

    def _drop_lease_commands(self, lease_id: uuid.UUID) -> list[PendingCommand]:
        dropped = self.commands.drop_lease(lease_id)
        if dropped:
            self._observe_unacked()
        return dropped

    def _observe_unacked(self) -> None:
        if self.metrics is not None:
            self.metrics.set_worker_commands_unacked(len(self.commands))

    def _note_sent(self, entry: PendingCommand) -> None:
        self.commands.mark_sent(entry)
        self.observe_command(entry.op, "sent")

    def _note_send_failed(self, entry: PendingCommand) -> None:
        self.commands.discard(entry)
        self._observe_unacked()
        self.observe_command(entry.op, "failed")

    def get(self, worker_id: uuid.UUID) -> WorkerConnection | None:
        return self._conns.get(worker_id)

    def features_of(self, worker_id: uuid.UUID | None) -> frozenset[str]:
        conn = self._conns.get(worker_id) if worker_id is not None else None
        return conn.features if conn is not None else BASELINE_FEATURES

    async def renew_on_activity(self, store: Store, conn: WorkerConnection) -> None:
        """Renew the worker's leases when it showed life without a heartbeat.

        Called after a committed ingest batch and on `lease.ack`. At most
        one renewal per heartbeat interval, so a busy socket costs one
        extra UPDATE per interval, not one per envelope.
        """
        now = time.monotonic()
        if now - conn.last_renewed < heartbeat_interval(self.settings):
            return
        conn.last_renewed = now
        async with store.session() as db:
            await extend_worker_leases(
                db,
                conn.worker_id,
                lease_until=utc_now() + self.settings.worker_lease_ttl,
                generation=conn.generation,
            )
        self.observe_lease_event("renewed")

    async def attach(self, conn: WorkerConnection) -> WorkerConnection | None:
        """Make the connection current and pickable.

        Register calls this only after `hello` was sent, so no command
        or revoke reaches a worker before its handshake. An older
        connection of the same worker is asked to close with the reason
        `takeover`.
        """
        conn.writer.bind(self.metrics, self.observe_send_queue)
        async with self._lock:
            previous = self._conns.get(conn.worker_id)
            self._conns[conn.worker_id] = conn
        if previous is not None and previous is not conn:
            previous.request_close("takeover")
            for lease_id in previous.leases:
                self.observe_lease_event("taken_over")
                log_event(
                    log,
                    logging.INFO,
                    "worker lease taken over",
                    event="worker.lease.taken_over",
                    worker_id=conn.worker_id,
                    lease_id=lease_id,
                    reason="reconnect",
                    previous_connection_id=previous.connection_id,
                )
        self._observe()
        return conn

    async def detach(
        self, worker_id: uuid.UUID, conn: WorkerConnection | None = None
    ) -> bool:
        """Drop the connection if it is still the current one; say whether it was."""
        async with self._lock:
            current = self._conns.get(worker_id)
            if current is None:
                return False
            if conn is not None and current is not conn:
                return False
            del self._conns[worker_id]
        self._observe()
        self.observe_send_queue()
        self._forget_worker_deltas(worker_id)
        return True

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
        metrics.set_worker_connections(counts, modes=self._metric_modes)

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
        payload: BaseCommandPayload | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if op not in COMMAND_OPS:
            raise ValueError(op)
        started = time.monotonic()
        request_id = _request_id(payload)
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
        payload: BaseCommandPayload | dict[str, Any] | None,
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
        image = session_image(session.environment) if required == "microvm" else None
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
            request_id = _request_id(payload)
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
        body = command_payload(op, payload, run_mode=required, image=image)
        command = WorkerCommand.build(command_id, session_id, lease_id, op, body)
        wire = command.to_wire()
        _check_command_context(op, wire)
        # Mark the command unacked before the grant commits, so an
        # inventory arriving between the grant and the send does not
        # orphan a lease whose command is still in flight.
        entry = self._enqueue(wire, conn)
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
                self._drop_lease_commands(lease_id)
                return None
            cursor = row.worker_seq
        if op in CURSOR_OPS:
            body = command_payload(op, body, run_mode=None, image=None, cursor=cursor)
            wire = WorkerCommand.build(
                command_id, session_id, lease_id, op, body
            ).to_wire()
            entry.wire = wire
        conn.leases.add(lease_id)
        self.observe_lease_event("granted")
        log_event(
            log,
            logging.INFO,
            "worker lease granted",
            event="worker.lease.granted",
            tenant_id=tenant_id,
            session_id=session_id,
            worker_id=conn.worker_id,
            lease_id=lease_id,
            command_id=command_id,
            op=op,
        )
        conn.lease_mem[lease_id] = session_mem
        self._note_delta_lease(
            session_id,
            worker_id=conn.worker_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
        )
        try:
            await self._send(conn, wire)
        except Exception:
            self._note_send_failed(entry)
            raise
        self._note_sent(entry)
        metrics = self.metrics
        if metrics is not None:
            metrics.worker_assign.observe(time.monotonic() - started)
        self._observe()
        return wire

    async def command(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        op: str,
        payload: BaseCommandPayload | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if op not in COMMAND_OPS:
            raise ValueError(op)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None or row.lease_id is None or row.worker_id is None:
                return None
            worker_id = row.worker_id
            lease_id = row.lease_id
            cursor = row.worker_seq
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
            follow_image = session_image(row.environment)
            if follow_image not in conn.images:
                raise ApiError(
                    "api_error",
                    image_unavailable_message(self, follow_image),
                    code="image_unavailable",
                    status_code=503,
                    session_id=str(session_id),
                )
        body = command_payload(
            op, payload, run_mode=required, image=follow_image, cursor=cursor
        )
        wire = WorkerCommand.build(
            uuid.uuid4(), session_id, lease_id, op, body
        ).to_wire()
        _check_command_context(op, wire)
        entry = self._enqueue(wire, conn)
        try:
            await self._send(conn, wire)
        except Exception:
            self._note_send_failed(entry)
            raise
        self._note_sent(entry)
        return wire

    async def ack(self, lease_id: uuid.UUID, command_id: str) -> bool:
        entry = self.commands.ack(lease_id, command_id)
        if entry is None:
            return False
        self._observe_unacked()
        if self.metrics is not None and entry.sent_at is not None:
            self.metrics.observe_worker_command_ack(
                entry.op, time.monotonic() - entry.sent_at
            )
        self.observe_command(entry.op, "acked")
        return True

    async def wait_ack(
        self, lease_id: uuid.UUID, command_id: str, *, timeout: float = 15
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.commands.get(lease_id, command_id) is None:
                return True
            await asyncio.sleep(0.05)
        entry = self.commands.get(lease_id, command_id)
        if entry is None:
            return True
        self.observe_command(entry.op, "timeout")
        self._warnings.warning(
            "worker command ack timed out",
            event="worker.command.ack_timeout",
            error_code="command_ack_timeout",
            lease_id=str(lease_id),
            command_id=command_id,
            op=entry.op,
        )
        return False

    def expect_stopped(self, session_id: uuid.UUID) -> "asyncio.Event":
        """Register to be told when the worker reports `session.stopped`."""
        event = self._stopped.setdefault(session_id, asyncio.Event())
        event.clear()
        return event

    def note_stopped(self, session_id: uuid.UUID) -> None:
        """Ingest applied a durable `session.stopped` for the session."""
        event = self._stopped.get(session_id)
        if event is not None:
            event.set()

    def forget_stopped(self, session_id: uuid.UUID) -> None:
        self._stopped.pop(session_id, None)

    async def release(
        self,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        lease_id: uuid.UUID,
    ) -> None:
        self._drop_lease_commands(lease_id)
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
        self.observe_lease_event("released")
        log_event(
            log,
            logging.INFO,
            "worker lease released",
            event="worker.lease.released",
            tenant_id=tenant_id,
            session_id=session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            reason="worker_release",
        )
        if worker_id is not None:
            conn = self._conns.get(worker_id)
            if conn is not None:
                conn.leases.discard(lease_id)
                conn.lease_mem.pop(lease_id, None)
        self._observe()

    async def expire(self, store: Store, hub: EventBus) -> list[uuid.UUID]:
        """Clear expired leases, then tell the connected workers.

        The rows are cleared and the failure events stored in one
        transaction. Everything that touches memory or a socket (the
        log lines, the delta state, the `lease.revoke` frames) happens
        after that transaction committed, so a failed commit changes
        nothing in memory and no socket write holds the row locks.
        """
        ttl = self.settings.worker_lease_ttl.total_seconds()
        cleared: list[tuple[Any, ...]] = []
        async with store.session() as db:
            rows = await list_expired_leases(db, utc_now())
            for row in rows:
                cleared.append(
                    (
                        row.id,
                        row.tenant_id,
                        row.worker_id,
                        row.lease_id,
                        row.lease_until,
                    )
                )
                await clear_session_lease(db, row.tenant_id, row.id)
                lease_failure = failure_for(
                    "worker_lease_expired", "Worker lease expired"
                )
                await persist_event(
                    db,
                    hub,
                    row.tenant_id,
                    row.id,
                    type="agent.session.error",
                    data=session_error_data(lease_failure, mode="legacy"),
                )
        lease_failure = failure_for("worker_lease_expired", "Worker lease expired")
        expired: list[uuid.UUID] = []
        revokes: list[tuple[WorkerConnection, LeaseRevoke]] = []
        for session_id, tenant_id, worker_id, lease_id, lease_until in cleared:
            since_renewal = None
            if lease_until is not None:
                if lease_until.tzinfo is None:
                    lease_until = lease_until.replace(tzinfo=UTC)
                since_renewal = round(
                    (utc_now() - lease_until).total_seconds() + ttl, 3
                )
            conn = self._conns.get(worker_id) if worker_id is not None else None
            heartbeat_age = None
            if conn is not None and conn.last_heartbeat is not None:
                heartbeat_age = round(time.monotonic() - conn.last_heartbeat, 3)
            log_event(
                log,
                logging.ERROR,
                "worker lease expired",
                event="worker.lease.expired",
                tenant_id=tenant_id,
                session_id=session_id,
                worker_id=worker_id,
                lease_ttl_seconds=ttl,
                last_renewal_age_seconds=since_renewal,
                last_heartbeat_age_seconds=heartbeat_age,
                worker_connected=conn is not None,
                **log_extra(lease_failure),
            )
            self.observe_lease_event("expired")
            expired.append(session_id)
            self._forget_delta(session_id)
            if lease_id is not None:
                self._drop_lease_commands(lease_id)
                if conn is not None:
                    conn.leases.discard(lease_id)
                    conn.lease_mem.pop(lease_id, None)
                    revokes.append(
                        (conn, LeaseRevoke(session_id=session_id, lease_id=lease_id))
                    )
        self._observe()
        if revokes:
            await asyncio.gather(
                *(self._send_revoke(conn, revoke) for conn, revoke in revokes)
            )
        return expired

    async def _send_revoke(self, conn: WorkerConnection, revoke: LeaseRevoke) -> None:
        try:
            await asyncio.wait_for(
                self._send(conn, revoke.to_wire()), timeout=REVOKE_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warnings.warning(
                "lease revoke not sent",
                event="worker.lease.revoke_failed",
                error_code="revoke_failed",
                worker_id=conn.worker_id,
                lease_id=revoke.lease_id,
                error=type(exc).__name__,
            )
            self.observe_protocol("revoke_failed")
            return
        self.observe_lease_event("revoked")

    async def replay(
        self,
        conn: WorkerConnection,
        store: Store,
        running: Sequence[RunningSession | dict[str, Any]] | None = None,
    ) -> dict[uuid.UUID, int]:
        sessions = await self.restore_leases(conn, store, running)
        await self.resend_pending(conn)
        return sessions

    async def restore_leases(
        self,
        conn: WorkerConnection,
        store: Store,
        running: Sequence[RunningSession | dict[str, Any]] | None = None,
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
        async with store.session() as db:
            rows = await list_worker_leases(db, conn.worker_id)
        claimed = claimed_leases(running)
        sessions: dict[uuid.UUID, int] = {}
        renewed: list[uuid.UUID] = []
        for row in rows:
            if row.lease_id is None:
                continue
            if (
                claimed
                and claimed.get(row.id) != row.lease_id
                and not self.commands.has_lease(row.lease_id)
            ):
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
            conn.last_renewed = time.monotonic()
            self.observe_lease_event("renewed")
        return sessions

    async def resend_pending(self, conn: WorkerConnection) -> None:
        """Send every unacked command of the worker's leases again, in order."""
        for lease_id in list(conn.leases):
            for entry in self.commands.for_lease(lease_id):
                await self._resend(conn, entry, reason="reconnect")

    async def _resend(
        self, conn: WorkerConnection, entry: PendingCommand, *, reason: str
    ) -> None:
        await self._send(conn, entry.wire)
        self.commands.mark_sent(entry)
        self.observe_command(entry.op, "retransmitted")
        conn.warnings.warning(
            "worker command retransmitted",
            event="worker.command.retransmitted",
            error_code="command_retransmitted",
            worker_id=conn.worker_id,
            lease_id=entry.lease_id,
            command_id=entry.command_id,
            op=entry.op,
            reason=reason,
            sends=entry.sends,
        )

    async def retransmit_due(self, store: Store, bus: EventBus) -> None:
        """Send unacked commands again on a timer and age out the old ones.

        A command that is still unacked after the lease TTL fails: the
        lease is cleared, the turn fails, and the worker is told to drop
        the lease. Every other unacked command is sent again every
        `retransmit_seconds` while its worker is connected.
        """
        ttl = self.settings.worker_lease_ttl.total_seconds()
        failed: set[uuid.UUID] = set()
        for entry in self.commands.expired(ttl):
            if entry.lease_id not in failed:
                failed.add(entry.lease_id)
                await self._expire_command(store, bus, entry)
        for entry in self.commands.due(self.retransmit_seconds):
            if entry.lease_id in failed or entry.worker_id is None:
                continue
            conn = self._conns.get(entry.worker_id)
            if conn is None or entry.lease_id not in conn.leases:
                continue
            try:
                await self._resend(conn, entry, reason="timer")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warnings.warning(
                    "worker command retransmit failed",
                    event="worker.command.retransmit_failed",
                    error_code="command_retransmit_failed",
                    worker_id=conn.worker_id,
                    lease_id=entry.lease_id,
                    error=type(exc).__name__,
                )

    async def _expire_command(
        self, store: Store, bus: EventBus, entry: PendingCommand
    ) -> None:
        dropped = self._drop_lease_commands(entry.lease_id)
        for item in dropped:
            self.observe_command(item.op, "expired")
        log_event(
            log,
            logging.ERROR,
            "worker command expired without an ack",
            event="worker.command.expired",
            error_code="worker_command_timeout",
            worker_id=entry.worker_id,
            session_id=entry.session_id,
            lease_id=entry.lease_id,
            command_id=entry.command_id,
            op=entry.op,
            sends=entry.sends,
            age_seconds=round(self.commands.clock() - entry.created, 3),
            queued=len(dropped),
        )
        failure = failure_for(
            "worker_command_timeout", "The worker did not acknowledge a command"
        )
        async with store.session() as db:
            row = await get_session_by_lease(db, entry.lease_id)
            if row is None:
                return
            tenant_id = row.tenant_id
            await clear_session_lease(db, tenant_id, row.id)
            await persist_event(
                db,
                bus,
                tenant_id,
                row.id,
                type="agent.session.error",
                data=session_error_data(failure, mode="legacy"),
            )
            await fail_stale_in_progress(db, bus, tenant_id, row.id)
        self._forget_delta(entry.session_id)
        conn = self._conns.get(entry.worker_id) if entry.worker_id else None
        if conn is not None:
            conn.leases.discard(entry.lease_id)
            conn.lease_mem.pop(entry.lease_id, None)
            await self._send_revoke(
                conn, LeaseRevoke(session_id=entry.session_id, lease_id=entry.lease_id)
            )
        self._observe()

    def _note_delta_lease(
        self,
        session_id: uuid.UUID,
        *,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> DeltaLease:
        return delta_gate._note_delta_lease(
            self,
            session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
        )

    def _forget_delta(self, session_id: uuid.UUID) -> None:
        delta_gate._forget_delta(self, session_id)

    def _forget_worker_deltas(self, worker_id: uuid.UUID) -> None:
        delta_gate._forget_worker_deltas(self, worker_id)

    async def handle_delta(
        self,
        store: Store,
        bus: EventBus,
        conn: WorkerConnection,
        envelope: WorkerEnvelope,
    ) -> bool:
        """Validate one ephemeral envelope and fan out its delta."""
        return await delta_gate.handle_delta(self, store, bus, conn, envelope)

    async def reconcile_inventory(
        self,
        store: Store,
        bus: EventBus,
        worker_id: uuid.UUID,
        reported: dict[uuid.UUID, uuid.UUID],
        unleased: Collection[uuid.UUID] = (),
        *,
        conn: WorkerConnection | None = None,
        orphan_missing: bool = True,
    ) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
        """Compare the worker live set against the lease rows."""
        return await inventory_gate.reconcile_inventory(
            self,
            store,
            bus,
            worker_id,
            reported,
            unleased,
            conn=conn,
            orphan_missing=orphan_missing,
        )

    def note_stored_events(
        self, session_id: uuid.UUID, bodies: Collection[dict[str, Any]]
    ) -> None:
        """Feed the delta gate with events ingest just stored."""
        delta_gate.note_stored_events(self, session_id, bodies)

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
