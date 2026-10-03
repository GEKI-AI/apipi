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
from apipi.common.logutil import log_event
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
    COMMAND_OPS,
    CURSOR_OPS,
    PUBLIC_EVENT_TYPES,
    BaseCommandPayload,
    LeaseRevoke,
    RunningSession,
    WorkerCommand,
    WorkerEnvelope,
)
from apipi.services.session_events import persist_event
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
from apipi.workerhub.commands import (
    _check_command_context,
    command_payload,
    image_unavailable_message,
    session_image,
)
from apipi.workerhub.connection import WorkerConnection, claimed_leases
from apipi.workerhub.deltas import DeltaLease
from apipi.workerhub.wire import close_socket, send_message, send_wire

log = logging.getLogger("apipi.worker")

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
        self._unacked: dict[uuid.UUID, dict[str, Any]] = {}
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

    def observe_lease_event(self, event: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_lease_event(event)

    def get(self, worker_id: uuid.UUID) -> WorkerConnection | None:
        return self._conns.get(worker_id)

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
            )
        self.observe_lease_event("renewed")

    async def attach(self, conn: WorkerConnection) -> WorkerConnection | None:
        async with self._lock:
            previous = self._conns.get(conn.worker_id)
            self._conns[conn.worker_id] = conn
        if previous is not None and previous is not conn:
            await close_socket(previous.websocket)
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
        _check_command_context(op, wire["payload"])
        # Mark the command unacked before the grant commits, so an
        # inventory arriving between the grant and the send does not
        # orphan a lease whose command is still in flight.
        self._unacked[lease_id] = wire
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
            cursor = row.worker_seq
        if op in CURSOR_OPS:
            body = command_payload(op, body, run_mode=None, image=None, cursor=cursor)
            wire = WorkerCommand.build(
                command_id, session_id, lease_id, op, body
            ).to_wire()
            if lease_id in self._unacked:
                self._unacked[lease_id] = wire
        conn.leases.add(lease_id)
        conn.lease_mem[lease_id] = session_mem
        self._note_delta_lease(
            session_id,
            worker_id=conn.worker_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
        )
        try:
            await send_wire(conn.websocket, wire)
        except Exception:
            self._unacked.pop(lease_id, None)
            raise
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
        _check_command_context(op, wire["payload"])
        self._unacked[lease_id] = wire
        try:
            await send_wire(conn.websocket, wire)
        except Exception:
            self._unacked.pop(lease_id, None)
            raise
        return wire

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
        self.observe_lease_event("released")
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
                lease_until = row.lease_until
                await clear_session_lease(db, row.tenant_id, row.id)
                lease_failure = failure_for(
                    "worker_lease_expired", "Worker lease expired"
                )
                ttl = self.settings.worker_lease_ttl.total_seconds()
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
                    tenant_id=row.tenant_id,
                    session_id=row.id,
                    worker_id=worker_id,
                    lease_ttl_seconds=ttl,
                    last_renewal_age_seconds=since_renewal,
                    last_heartbeat_age_seconds=heartbeat_age,
                    worker_connected=conn is not None,
                    **log_extra(lease_failure),
                )
                self.observe_lease_event("expired")
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
                    if conn is not None:
                        conn.leases.discard(lease_id)
                        conn.lease_mem.pop(lease_id, None)
                        await send_message(
                            conn.websocket,
                            LeaseRevoke(session_id=row.id, lease_id=lease_id),
                        )
        self._observe()
        return expired

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
            conn.last_renewed = time.monotonic()
            self.observe_lease_event("renewed")
        return sessions

    async def resend_pending(self, conn: WorkerConnection) -> None:
        for lease_id in conn.leases:
            pending = self._unacked.get(lease_id)
            if pending is not None:
                await send_wire(conn.websocket, pending)

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
    ) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
        """Compare the worker live set against the lease rows."""
        return await inventory_gate.reconcile_inventory(
            self, store, bus, worker_id, reported, unleased
        )

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
