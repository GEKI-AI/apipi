import asyncio
import contextlib
import json
import logging
import secrets
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from starlette.websockets import WebSocket, WebSocketState

from apipi.config import ConfigError, Settings
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.otel import (
    Tracing,
    attach_traceparent,
    detach_traceparent,
    start_span,
)
from apipi.services.runtime import PUBLIC_EVENT_TYPES, EventHub, persist_event
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    clear_session_lease,
    extend_worker_leases,
    get_session,
    get_session_by_lease,
    list_expired_leases,
    list_worker_leases,
    set_session_lease,
    touch_worker,
    upsert_worker,
)
from apipi.worker.pi.sandbox import mem_mib_for_size, sandbox_size_of
from apipi.worker.placement import placement_for, worker_accepts

COMMAND_OPS = frozenset({"turn.start", "turn.cancel", "turn.continue", "session.stop"})
WORKER_IN = frozenset({"register", "heartbeat", "lease.ack", "lease.release", "event"})
log = logging.getLogger("apipi.worker")


@dataclass
class WorkerConnection:
    worker_id: uuid.UUID
    generation: int
    websocket: WebSocket
    capacity: int
    memory_mb: int
    run_mode: str
    leases: set[uuid.UUID] = field(default_factory=set)
    lease_mem: dict[uuid.UUID, int] = field(default_factory=dict)
    draining: bool = False


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
        self._metric_modes: set[str] = set()
        self._lock = asyncio.Lock()

    def authorized(self, token: str | None) -> bool:
        expected = self.settings.worker_token
        if expected is None or expected == "" or token is None:
            return False
        return secrets.compare_digest(token, expected)

    def live(self) -> int:
        return len(self._conns)

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
        self._metric_modes |= {"chat", "microvm", "none"}
        metrics.set_workers(counts, leases, modes=self._metric_modes)

    def pick(
        self, session_mem_mib: int | None = None, *, run_mode: str
    ) -> WorkerConnection | None:
        session_mem = (
            session_mem_mib
            if session_mem_mib is not None
            else self.settings.microvm_mem_mib
        )
        ready = []
        for conn in self._conns.values():
            if conn.run_mode != run_mode:
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
        required = placement_for(
            environment=session.environment,
            metadata=session.metadata_json,
            env_none=self.settings.env_none_placement,
        )
        if required is None:
            request_id = (
                payload.get("request_id") if isinstance(payload, dict) else None
            )
            log_event(
                log,
                logging.WARNING,
                "worker assign failed",
                event="worker.assign.failed",
                error_code="placement",
                tenant_id=tenant_id,
                session_id=session_id,
                request_id=request_id,
            )
            raise ApiError(
                "invalid_request",
                "environment.type=none is rejected by APIPI_ENV_NONE_PLACEMENT",
                code="placement",
                status_code=400,
            )
        session_mem = mem_mib_for_size(
            self.settings, sandbox_size_of(session.environment)
        )
        conn = self.pick(session_mem, run_mode=required)
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
                return None
        conn.leases.add(lease_id)
        conn.lease_mem[lease_id] = session_mem
        command = {
            "type": "command",
            "id": str(command_id),
            "session_id": str(session_id),
            "lease_id": str(lease_id),
            "op": op,
            "payload": _payload_with_run_mode(payload, required),
        }
        self._unacked[lease_id] = command
        await _send(conn.websocket, command)
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
            required = placement_for(
                environment=row.environment,
                metadata=row.metadata_json,
                env_none=self.settings.env_none_placement,
            )
        conn = self._conns.get(worker_id)
        if conn is None or lease_id not in conn.leases:
            return None
        command_id = uuid.uuid4()
        command = {
            "type": "command",
            "id": str(command_id),
            "session_id": str(session_id),
            "lease_id": str(lease_id),
            "op": op,
            "payload": _payload_with_run_mode(payload, required),
        }
        self._unacked[lease_id] = command
        await _send(conn.websocket, command)
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
            if row is None or row.lease_id != lease_id:
                return
            worker_id = row.worker_id
            await clear_session_lease(db, tenant_id, session_id)
        if worker_id is not None:
            conn = self._conns.get(worker_id)
            if conn is not None:
                conn.leases.discard(lease_id)
                conn.lease_mem.pop(lease_id, None)
        self._observe()

    async def expire(self, store: Store, hub: EventHub) -> list[uuid.UUID]:
        expired: list[uuid.UUID] = []
        async with store.session() as db:
            rows = await list_expired_leases(db, utc_now())
            for row in rows:
                lease_id = row.lease_id
                worker_id = row.worker_id
                await clear_session_lease(db, row.tenant_id, row.id)
                log_event(
                    log,
                    logging.WARNING,
                    "worker lease expired",
                    event="worker.lease.expired",
                    error_code="worker_lease_expired",
                    tenant_id=row.tenant_id,
                    session_id=row.id,
                    worker_id=worker_id,
                )
                await persist_event(
                    db,
                    hub,
                    row.tenant_id,
                    row.id,
                    type="agent.session.error",
                    data={
                        "message": "Worker lease expired",
                        "code": "worker_lease_expired",
                    },
                )
                expired.append(row.id)
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

    async def replay(self, conn: WorkerConnection, store: Store) -> None:
        async with store.session() as db:
            rows = await list_worker_leases(db, conn.worker_id)
        for row in rows:
            if row.lease_id is None:
                continue
            conn.leases.add(row.lease_id)
            conn.lease_mem[row.lease_id] = mem_mib_for_size(
                self.settings, sandbox_size_of(row.environment)
            )
            pending = self._unacked.get(row.lease_id)
            if pending is not None:
                await _send(conn.websocket, pending)

    async def handle_event(
        self,
        store: Store,
        hub: EventHub,
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


def _payload_with_run_mode(
    payload: dict[str, Any] | None, required: str | None
) -> dict[str, Any]:
    out = dict(payload) if payload is not None else {}
    if required is not None:
        out["run_mode"] = required
    return out


async def register_worker(
    hub: WorkerHub, store: Store, websocket: WebSocket, message: dict[str, Any]
) -> WorkerConnection | None:
    raw_id = message.get("id")
    run_mode = _run_mode(message.get("run_mode"))
    if run_mode is None:
        return None
    capacity = _positive_int(message.get("capacity", 1))
    if capacity is None:
        return None
    raw_memory = message.get("memory_mb")
    if raw_memory is None:
        memory_mb = capacity * hub.settings.microvm_mem_mib
    else:
        parsed = _positive_int(raw_memory)
        if parsed is None:
            return None
        memory_mb = parsed
    if isinstance(raw_id, str) and raw_id:
        try:
            worker_id = uuid.UUID(raw_id)
        except ValueError:
            return None
    else:
        worker_id = uuid.uuid4()
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
    )
    await hub.attach(conn)
    await _send(
        websocket,
        {
            "type": "hello",
            "ok": True,
            "worker_id": str(conn.worker_id),
            "generation": conn.generation,
        },
    )
    await hub.replay(conn, store)
    return conn


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


async def _close(websocket: WebSocket) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        await websocket.close()


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
    if store is None or hub is None:
        return
    async with store.session() as db:
        await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.error",
            data={
                "message": "Worker run_mode does not match the session",
                "code": "placement",
            },
        )
        await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.failed",
            data={"message": "Worker run_mode does not match the session"},
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
    request_id = request_id if isinstance(request_id, str) else None
    api_key = api_key if isinstance(api_key, str) else None
    key_id = key_id if isinstance(key_id, str) else None
    user_id = user_id if isinstance(user_id, str) else None
    thinking_summary = payload.get("thinking_summary") is True
    auto_title = payload.get("auto_title") is True
    raw_parent = payload.get("traceparent")
    token = attach_traceparent(raw_parent if isinstance(raw_parent, str) else None)
    try:
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
            thinking_summary=thinking_summary,
            auto_title=auto_title,
        )
    except ApiError as exc:
        if exc.status_code >= 500:
            log_event(
                log,
                logging.ERROR,
                "worker command failed",
                event="worker.command.failed",
                error_code=exc.code or "internal",
                exc_info=exc,
                tenant_id=tenant_id,
                session_id=session_id,
                request_id=request_id,
            )
        raise
    except Exception as exc:
        log_event(
            log,
            logging.ERROR,
            "worker command failed",
            event="worker.command.failed",
            error_code="internal",
            exc_info=exc,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
        )
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
    thinking_summary: bool = False,
    auto_title: bool = False,
) -> None:
    if op == "turn.start":
        required = payload.get("run_mode")
        worker_mode = getattr(getattr(execution, "settings", None), "run_mode", None)
        if (
            isinstance(required, str)
            and isinstance(worker_mode, str)
            and not worker_accepts(worker_mode, required)
        ):
            await _reject_mismatched_turn(
                execution,
                tenant_id,
                session_id,
                required=required,
                worker_mode=worker_mode,
                request_id=request_id,
            )
            return
        text = payload.get("text")
        await execution.run_turn(
            tenant_id,
            session_id,
            text if isinstance(text, str) else "",
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            thinking_summary=thinking_summary,
            auto_title=auto_title,
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
            thinking_summary=thinking_summary,
            auto_title=auto_title,
        )
        return
    if op == "turn.cancel":
        await execution.cancel(session_id, status="in_progress")
        return
    if op == "session.stop":
        await execution.teardown(session_id)
        await _wipe_stopped_session(execution, tenant_id, session_id)


async def _wipe_stopped_session(
    execution: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    store = getattr(execution, "store", None)
    settings = getattr(execution, "settings", None)
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


def worker_heartbeat(settings: Settings, *, drain: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "heartbeat",
        "capacity": settings.max_sessions,
        "memory_mb": settings.node_memory_mb(),
        "run_mode": settings.run_mode,
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
    from apipi.store.engine import Store, create_engine
    from apipi.worker.execution import local_execution, worker_observability
    from apipi.worker.pi.orphan import sweep_host_orphans

    token = settings.worker_token
    if token is None or token == "":
        raise ConfigError("APIPI_WORKER_TOKEN is required")
    base = url or settings.api_url or "http://127.0.0.1:8000"
    ws_url = worker_ws_url(base)
    heartbeat = min(10.0, max(1.0, settings.worker_lease_ttl.total_seconds() / 2))
    store = Store(create_engine(settings.database_url, pool_size=settings.db_pool_size))
    metrics, tracing = worker_observability(settings)
    execution = local_execution(settings, store=store, metrics=metrics, tracing=tracing)
    if execution.stdio_on_host:
        await sweep_host_orphans()
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
    log.info("worker connect", extra={"url": ws_url})
    draining = asyncio.Event()
    _install_drain_signals(draining)
    drain_deadline: float | None = None
    wait = drain_timeout_seconds(settings, drain_timeout)
    command_tasks: set[asyncio.Task[None]] = set()
    status = 0
    try:
        async with websockets.connect(
            ws_url, additional_headers={"Authorization": f"Bearer {token}"}
        ) as sock:
            await sock.send(
                json.dumps(
                    {
                        "type": "register",
                        "capacity": settings.max_sessions,
                        "memory_mb": settings.node_memory_mb(),
                        "run_mode": settings.run_mode,
                    }
                )
            )
            raw = await sock.recv()
            hello = (
                json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
            )
            if not isinstance(hello, dict) or not hello.get("ok"):
                error = (
                    hello.get("error") if isinstance(hello, dict) else "unauthorized"
                )
                raise ConfigError(f"worker register failed: {error}")
            log.info("worker hello", extra={"worker_id": hello.get("worker_id")})

            async def send_heartbeat() -> None:
                await sock.send(
                    json.dumps(worker_heartbeat(settings, drain=draining.is_set()))
                )

            while True:
                if draining.is_set() and drain_deadline is None:
                    drain_deadline = time.monotonic() + wait
                    log.info("worker drain")
                    await send_heartbeat()
                    await execution.pool.kill_unheld(reason="idle")
                    if drain_idle(execution.pool.live(), command_tasks):
                        break
                recv_timeout = 0.5 if draining.is_set() else heartbeat
                try:
                    incoming = await asyncio.wait_for(sock.recv(), timeout=recv_timeout)
                except TimeoutError:
                    await send_heartbeat()
                    if draining.is_set():
                        await execution.pool.kill_unheld(reason="idle")
                        if drain_idle(execution.pool.live(), command_tasks):
                            break
                        if (
                            drain_deadline is not None
                            and time.monotonic() >= drain_deadline
                        ):
                            status = 1
                            break
                    continue
                text = incoming if isinstance(incoming, str) else incoming.decode()
                message = json.loads(text)
                if not isinstance(message, dict):
                    continue
                if (
                    message.get("type") == "command"
                    and message.get("op") == "session.stop"
                ):
                    await dispatch_command(execution, message)
                    await sock.send(
                        json.dumps(
                            {
                                "type": "lease.ack",
                                "id": message.get("id"),
                                "lease_id": message.get("lease_id"),
                            }
                        )
                    )
                    continue
                if message.get("type") == "command":
                    await sock.send(
                        json.dumps(
                            {
                                "type": "lease.ack",
                                "id": message.get("id"),
                                "lease_id": message.get("lease_id"),
                            }
                        )
                    )
                    log.info(
                        "worker command",
                        extra={
                            "op": message.get("op"),
                            "session_id": message.get("session_id"),
                        },
                    )
                    task = asyncio.create_task(dispatch_command(execution, message))
                    command_tasks.add(task)
                    task.add_done_callback(command_tasks.discard)
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        await execution.close()
        if execution.tracing is not None:
            execution.tracing.shutdown()
        await store.dispose()
    return status
