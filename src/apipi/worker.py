import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from starlette.websockets import WebSocket, WebSocketState

from apipi.config import ConfigError, Settings
from apipi.runtime import PUBLIC_EVENT_TYPES, EventHub, persist_event
from apipi.sandbox import mem_mib_for_size, sandbox_size_of
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

COMMAND_OPS = frozenset({"turn.start", "turn.cancel", "turn.continue"})
WORKER_IN = frozenset({"register", "heartbeat", "lease.ack", "lease.release", "event"})
log = logging.getLogger("apipi.worker")


@dataclass
class WorkerConnection:
    worker_id: uuid.UUID
    generation: int
    websocket: WebSocket
    capacity: int
    memory_mb: int
    leases: set[uuid.UUID] = field(default_factory=set)
    lease_mem: dict[uuid.UUID, int] = field(default_factory=dict)
    draining: bool = False


class WorkerHub:
    def __init__(self, settings: Settings, *, metrics: Any | None = None) -> None:
        self.settings = settings
        self.metrics = metrics
        self._conns: dict[uuid.UUID, WorkerConnection] = {}
        self._unacked: dict[uuid.UUID, dict[str, Any]] = {}
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
        metrics.workers.set(len(self._conns))
        metrics.worker_leases.set(
            sum(len(conn.leases) for conn in self._conns.values())
        )

    def pick(self, session_mem_mib: int | None = None) -> WorkerConnection | None:
        session_mem = (
            session_mem_mib
            if session_mem_mib is not None
            else self.settings.microvm_mem_mib
        )
        ready = []
        for conn in self._conns.values():
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
        async with store.session() as db:
            session = await get_session(db, tenant_id, session_id)
        session_mem = mem_mib_for_size(
            self.settings, sandbox_size_of(session.environment if session else None)
        )
        conn = self.pick(session_mem)
        if conn is None:
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
            "payload": payload if payload is not None else {},
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
            "payload": payload if payload is not None else {},
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


async def register_worker(
    hub: WorkerHub, store: Store, websocket: WebSocket, message: dict[str, Any]
) -> WorkerConnection | None:
    raw_id = message.get("id")
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
    request_id = request_id if isinstance(request_id, str) else None
    api_key = api_key if isinstance(api_key, str) else None
    key_id = key_id if isinstance(key_id, str) else None
    if op == "turn.start":
        text = payload.get("text")
        await execution.run_turn(
            tenant_id,
            session_id,
            text if isinstance(text, str) else "",
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
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
        )
        return
    if op == "turn.cancel":
        await execution.cancel(session_id, status="in_progress")


def worker_ws_url(base: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme in {"http", "https"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        parsed = parsed._replace(scheme=scheme)
    elif parsed.scheme not in {"ws", "wss"}:
        raise ConfigError("APIPI_API_URL must be an http URL")
    path = parsed.path.rstrip("/") + "/internal/worker"
    return urlunparse(parsed._replace(path=path, fragment=""))


async def run_worker(settings: Settings, *, url: str | None = None) -> None:
    from apipi.execution import local_execution
    from apipi.store.engine import Store, create_engine

    token = settings.worker_token
    if token is None or token == "":
        raise ConfigError("APIPI_WORKER_TOKEN is required")
    base = url or settings.api_url or "http://127.0.0.1:8000"
    ws_url = worker_ws_url(base)
    heartbeat = min(10.0, max(1.0, settings.worker_lease_ttl.total_seconds() / 2))
    store = Store(create_engine(settings.database_url, pool_size=settings.db_pool_size))
    execution = local_execution(settings, store=store)
    tasks: set[asyncio.Task[None]] = set()
    log.info("worker connect", extra={"url": ws_url})
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
            while True:
                try:
                    incoming = await asyncio.wait_for(sock.recv(), timeout=heartbeat)
                except TimeoutError:
                    await sock.send(
                        json.dumps(
                            {
                                "type": "heartbeat",
                                "capacity": settings.max_sessions,
                                "memory_mb": settings.node_memory_mb(),
                            }
                        )
                    )
                    continue
                text = incoming if isinstance(incoming, str) else incoming.decode()
                message = json.loads(text)
                if not isinstance(message, dict):
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
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        await execution.close()
        await store.dispose()
