import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from apipi.config import Settings
from apipi.worker.client import _serve_connection
from apipi.worker.commands import CommandDedupe
from apipi.worker.deltas import DeltaRelay
from apipi.worker.outbox import Outbox


def hello_frame(**extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "worker_id": str(uuid.uuid4()),
        "lease_ttl_seconds": 30,
        "heartbeat_seconds": 0.05,
        "sessions": {},
        **extra,
    }


class FakeSock:
    def __init__(self, incoming: list[Any] | None = None) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        for message in incoming or []:
            self.push(message)

    def push(self, message: Any) -> None:
        self.queue.put_nowait(
            json.dumps(message) if isinstance(message, dict) else message
        )

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> Any:
        return await self.queue.get()

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == kind]


class _Relay(DeltaRelay):
    def __init__(self) -> None:
        super().__init__()
        self.forgotten: list[uuid.UUID] = []

    def forget(self, session_id: uuid.UUID) -> None:
        self.forgotten.append(session_id)


class _Pool:
    def __init__(self) -> None:
        self.live_ids: set[uuid.UUID] = set()
        self.closed = False

    def live(self) -> int:
        return len(self.live_ids)

    def alive(self, session_id: uuid.UUID) -> bool:
        return session_id in self.live_ids

    def held(self, session_id: uuid.UUID) -> bool:
        return False

    def hold(self, session_id: uuid.UUID) -> None:
        del session_id

    def release(self, session_id: uuid.UUID) -> None:
        del session_id

    async def after_turn(self, session_id: uuid.UUID) -> None:
        del session_id

    async def kill_unheld(self, reason: str = "") -> None:
        del reason

    async def close(self) -> None:
        self.closed = True


class FakeExecution:
    tracing = None

    def __init__(self, settings: Settings, outbox: Outbox) -> None:
        self.settings = settings
        self.outbox = outbox
        self.pool = _Pool()
        self.metrics: Any = None
        self.presign_waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
        self.turns: list[uuid.UUID] = []
        self.cancels: list[uuid.UUID] = []
        self.teardowns: list[uuid.UUID] = []
        self.seen_hook: Any = None
        self.note_stopped: Any = None
        self.socket_open = False
        self._context_ttl: dict[str, Any] = {}
        self.on_teardown: Any = None
        self.on_cancel: Any = None

    async def run_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID, text: str, **kwargs: Any
    ) -> None:
        del tenant_id, text, kwargs
        self.turns.append(session_id)

    async def cancel(self, session_id: uuid.UUID, *, status: str) -> None:
        del status
        self.cancels.append(session_id)
        if self.on_cancel is not None:
            await self.on_cancel(session_id)

    def sink_for(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        return None

    async def teardown(self, session_id: uuid.UUID) -> None:
        self.teardowns.append(session_id)
        if self.on_teardown is not None:
            await self.on_teardown(session_id)


def command_frame(
    session_id: uuid.UUID,
    lease_id: str,
    op: str = "turn.start",
    *,
    command_id: uuid.UUID | None = None,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "type": "command",
        "id": str(command_id or uuid.uuid4()),
        "session_id": str(session_id),
        "lease_id": lease_id,
        "op": op,
        "payload": {
            "tenant_id": str(uuid.uuid4()),
            "text": "hi",
            "last_seq": 0,
            **payload,
        },
    }


@dataclass
class Connection:
    task: "asyncio.Task[tuple[str, float | None]]"
    sock: FakeSock
    execution: FakeExecution
    outbox: Outbox
    leases: dict[uuid.UUID, str]
    dedupe: CommandDedupe
    draining: asyncio.Event
    pending: dict[uuid.UUID, str]
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def stop(self) -> None:
        self.task.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(self.task, *self.tasks, return_exceptions=True)


def start_connection(
    settings: Settings,
    sock: FakeSock,
    *,
    execution: FakeExecution | None = None,
    outbox: Outbox | None = None,
    leases: dict[uuid.UUID, str] | None = None,
    pending: dict[uuid.UUID, str] | None = None,
    draining: asyncio.Event | None = None,
    wait: float = 30.0,
) -> Connection:
    outbox = outbox if outbox is not None else Outbox()
    execution = execution if execution is not None else FakeExecution(settings, outbox)
    leases = leases if leases is not None else {}
    dedupe = CommandDedupe()
    draining = draining if draining is not None else asyncio.Event()
    pending = pending if pending is not None else {}
    tasks: set[asyncio.Task[None]] = set()
    task = asyncio.create_task(
        _serve_connection(
            settings,
            execution,
            outbox,
            _Relay(),
            sock,
            leases,
            set(),
            tasks,
            draining,
            None,
            wait,
            None,
            dedupe,
            pending_releases=pending,
        )
    )
    return Connection(
        task, sock, execution, outbox, leases, dedupe, draining, pending, tasks
    )
