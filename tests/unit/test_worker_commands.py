"""Duplicate commands are acked but dispatched once (#449)."""

import asyncio
import json
import uuid
from collections import deque
from typing import Any


class _Sock:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self._incoming: deque[dict[str, Any]] = deque(incoming)
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        while not self._incoming:
            await asyncio.sleep(0.01)
        return json.dumps(self._incoming.popleft())


class _Relay:
    def __init__(self) -> None:
        self.forgotten: list[uuid.UUID] = []

    def attach(self, _send: Any) -> None:
        return None

    def detach(self) -> None:
        return None

    def forget(self, session_id: uuid.UUID) -> None:
        self.forgotten.append(session_id)


class _Pool:
    def live(self) -> int:
        return 0

    async def kill_unheld(self, reason: str = "") -> None:
        del reason
        return None


class _Execution:
    def __init__(self) -> None:
        self.turns: list[tuple[uuid.UUID, str]] = []
        self.stops = 0
        self.pool = _Pool()
        self.seen_hook: Any = None
        self.note_stopped: Any = None
        self._context_ttl: dict[str, Any] = {}

    async def run_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID, text: str, **kwargs: Any
    ) -> None:
        del kwargs
        self.turns.append((session_id, text))

    async def teardown(self, session_id: uuid.UUID) -> None:
        del session_id
        self.stops += 1


def _command(
    session_id: uuid.UUID, command_id: uuid.UUID, lease_id: str, op: str = "turn.start"
) -> dict[str, Any]:
    return {
        "type": "command",
        "id": str(command_id),
        "session_id": str(session_id),
        "lease_id": lease_id,
        "op": op,
        "payload": {"tenant_id": str(uuid.uuid4()), "text": "hi"},
    }


async def test_duplicate_turn_start_dispatched_once(settings) -> None:
    from apipi.worker.hub import _serve_connection
    from apipi.worker.outbox import Outbox

    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    command_id = uuid.uuid4()
    hello = {"ok": True, "worker_id": str(uuid.uuid4()), "sessions": {}}
    first = _command(session_id, command_id, lease_id)
    sock = _Sock([hello, first, dict(first)])
    execution = _Execution()
    outbox = Outbox()
    relay = _Relay()
    command_tasks: set[asyncio.Task[None]] = set()
    tasks: set[asyncio.Task[None]] = set()
    draining = asyncio.Event()
    server = asyncio.create_task(
        _serve_connection(
            settings,
            execution,
            outbox,
            relay,
            sock,  # type: ignore[arg-type]
            {},
            command_tasks,
            tasks,
            draining,
            None,
            30.0,
            0.05,
            None,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            acks = [
                message for message in sock.sent if message.get("type") == "lease.ack"
            ]
            if len(acks) >= 2 and execution.turns:
                break
            await asyncio.sleep(0.02)
        assert len(execution.turns) == 1
        assert execution.turns[0][0] == session_id
        acks = [message for message in sock.sent if message.get("type") == "lease.ack"]
        assert len(acks) == 2
        assert all(ack["id"] == str(command_id) for ack in acks)
    finally:
        server.cancel()
        for task in list(command_tasks):
            task.cancel()


async def test_inventory_reply_applies_revoke_and_ttl(settings) -> None:
    from apipi.worker.hub import _serve_connection
    from apipi.worker.outbox import Outbox

    revoked = uuid.uuid4()
    hello = {
        "ok": True,
        "worker_id": str(uuid.uuid4()),
        "sessions": {},
        "revoke": [{"session_id": str(revoked), "lease_id": str(uuid.uuid4())}],
        "ttl": {},
    }
    reply = {
        "type": "inventory.reply",
        "revoke": [{"session_id": str(revoked), "lease_id": str(uuid.uuid4())}],
        "ttl": {},
    }
    sock = _Sock([hello, reply])
    execution = _Execution()
    outbox = Outbox()
    relay = _Relay()
    session_leases = {revoked: str(uuid.uuid4())}
    command_tasks: set[asyncio.Task[None]] = set()
    tasks: set[asyncio.Task[None]] = set()
    server = asyncio.create_task(
        _serve_connection(
            settings,
            execution,
            outbox,
            relay,
            sock,  # type: ignore[arg-type]
            session_leases,
            command_tasks,
            tasks,
            draining := asyncio.Event(),
            None,
            30.0,
            0.05,
            None,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if execution.stops:
                break
            await asyncio.sleep(0.02)
        assert execution.stops >= 1
        assert revoked not in session_leases
        assert revoked in relay.forgotten
    finally:
        server.cancel()
        del draining
