"""Duplicate commands are acked but dispatched once (#449)."""

import asyncio
import json
import uuid
from collections import deque
from typing import Any

from apipi.worker.hub import CommandDedupe


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
    def __init__(self) -> None:
        self.live_ids: set[uuid.UUID] = set()

    def live(self) -> int:
        return len(self.live_ids)

    def alive(self, session_id: uuid.UUID) -> bool:
        return session_id in self.live_ids

    def held(self, session_id: uuid.UUID) -> bool:
        return False

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

    def sink_for(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        return None

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
            CommandDedupe(),
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
            CommandDedupe(),
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


async def _serve(
    settings,
    execution,
    outbox,
    relay,
    sock,
    session_leases,
    command_tasks,
    tasks,
    dedupe,
):
    from apipi.worker.hub import _serve_connection

    return asyncio.create_task(
        _serve_connection(
            settings,
            execution,
            outbox,
            relay,
            sock,  # type: ignore[arg-type]
            session_leases,
            command_tasks,
            tasks,
            asyncio.Event(),
            None,
            30.0,
            0.05,
            None,
            dedupe,
        )
    )


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


async def test_reconnect_replay_dispatched_once(settings) -> None:
    """A reconnect replays the same command_id; only the first dispatches."""
    from apipi.worker.outbox import Outbox

    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    command_id = uuid.uuid4()
    hello = {"ok": True, "worker_id": str(uuid.uuid4()), "sessions": {}}
    command = _command(session_id, command_id, lease_id)
    execution = _Execution()
    outbox = Outbox()
    relay = _Relay()
    session_leases: dict[uuid.UUID, str] = {}
    dedupe = CommandDedupe()
    command_tasks: set[asyncio.Task[None]] = set()
    tasks: set[asyncio.Task[None]] = set()
    first_sock = _Sock([hello, command])
    first = await _serve(
        settings,
        execution,
        outbox,
        relay,
        first_sock,
        session_leases,
        command_tasks,
        tasks,
        dedupe,
    )
    try:
        await _wait_for(lambda: len(execution.turns) == 1)
    finally:
        first.cancel()
        for task in list(command_tasks):
            task.cancel()
    assert session_leases == {session_id: lease_id}
    reconnect_hello = {
        "ok": True,
        "worker_id": str(uuid.uuid4()),
        "sessions": {str(session_id): 0},
        "revoke": [],
        "ttl": {},
    }
    second_sock = _Sock([reconnect_hello, dict(command)])
    command_tasks.clear()
    second = await _serve(
        settings,
        execution,
        outbox,
        relay,
        second_sock,
        session_leases,
        command_tasks,
        tasks,
        dedupe,
    )
    try:
        await _wait_for(
            lambda: any(
                message.get("type") == "lease.ack"
                and message.get("id") == str(command_id)
                for message in second_sock.sent
            )
        )
        await asyncio.sleep(0.2)
        assert len(execution.turns) == 1
    finally:
        second.cancel()
        for task in list(command_tasks):
            task.cancel()


async def test_unleased_session_dirs_lists_unknown(settings) -> None:
    from pathlib import Path

    from apipi.worker.hub import _unleased_session_dirs

    root = Path(str(settings.sessions_dir))
    tenant_dir = root / str(uuid.uuid4())
    known = uuid.uuid4()
    unknown = uuid.uuid4()
    live = uuid.uuid4()
    for session_id in (known, unknown, live):
        workspace = tenant_dir / str(session_id)
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("x")
    (tenant_dir / "not-a-session").mkdir()
    pool = _Pool()
    pool.live_ids.add(live)
    found = _unleased_session_dirs(settings, {known: "lease"}, pool)
    assert found == [unknown]


async def test_apply_inventory_reply_wipes_only_unknown(settings) -> None:
    from pathlib import Path

    from apipi.worker.hub import _apply_inventory_reply
    from apipi.worker.outbox import Outbox

    root = Path(str(settings.sessions_dir))
    tenant_dir = root / str(uuid.uuid4())
    known = uuid.uuid4()
    unknown = uuid.uuid4()
    for session_id in (known, unknown):
        workspace = tenant_dir / str(session_id)
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("x")
    execution = _Execution()
    outbox = Outbox()
    relay = _Relay()
    session_leases = {known: str(uuid.uuid4())}
    dedupe = CommandDedupe()
    assert dedupe.duplicate(unknown, "cmd") is False
    await _apply_inventory_reply(
        execution,
        {
            "type": "inventory.reply",
            "revoke": [
                {"session_id": str(known)},
                {"session_id": str(unknown)},
            ],
            "ttl": {},
        },
        session_leases=session_leases,
        outbox=outbox,
        relay=relay,
        dedupe=dedupe,
        settings=settings,
    )
    assert execution.stops == 2
    assert known in relay.forgotten and unknown in relay.forgotten
    assert (tenant_dir / str(known)).is_dir()
    assert not (tenant_dir / str(unknown)).exists()
    assert outbox.pending(known) == []
    reaped = outbox.pending(unknown)
    assert len(reaped) == 1
    assert reaped[0]["type"] == "workspace.reaped"
    assert dedupe.duplicate(unknown, "cmd") is False
