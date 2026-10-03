"""Duplicate commands are acked but dispatched once (#449)."""

import asyncio
import json
import uuid
from collections import deque
from typing import Any

import pytest

from apipi.worker.hub import CommandDedupe

HELLO_BASE = {"ok": True, "lease_ttl_seconds": 30, "heartbeat_seconds": 0.05}


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
        self.metrics: Any = None
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
        "payload": {"tenant_id": str(uuid.uuid4()), "text": "hi", "last_seq": 0},
    }


async def test_duplicate_turn_start_dispatched_once(settings) -> None:
    from apipi.worker.hub import _serve_connection
    from apipi.worker.outbox import Outbox

    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    command_id = uuid.uuid4()
    hello = {**HELLO_BASE, "worker_id": str(uuid.uuid4()), "sessions": {}}
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
        **HELLO_BASE,
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
    hello = {**HELLO_BASE, "worker_id": str(uuid.uuid4()), "sessions": {}}
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
        **HELLO_BASE,
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


class _BusySock(_Sock):
    """A socket that is never quiet: one ack arrives every few milliseconds."""

    def __init__(self, hello: dict[str, Any]) -> None:
        super().__init__([hello])
        self.session_id = uuid.uuid4()

    async def recv(self) -> str:
        if self._incoming:
            return json.dumps(self._incoming.popleft())
        await asyncio.sleep(0.005)
        return json.dumps(
            {"type": "ack", "session_id": str(self.session_id), "last_seq": 0}
        )


def _sent_types(sock: _Sock, kind: str) -> list[dict[str, Any]]:
    return [message for message in sock.sent if message.get("type") == kind]


async def _start(settings, execution, sock, **kwargs):
    from apipi.worker.outbox import Outbox

    outbox = kwargs.pop("outbox", None) or Outbox()
    session_leases = kwargs.pop("session_leases", {})
    command_tasks: set[asyncio.Task[None]] = set()
    tasks: set[asyncio.Task[None]] = set()
    dedupe = CommandDedupe()
    task = await _serve(
        settings,
        execution,
        outbox,
        _Relay(),
        sock,
        session_leases,
        command_tasks,
        tasks,
        dedupe,
    )
    return task, outbox, session_leases, command_tasks


async def test_heartbeat_runs_on_a_timer_when_the_socket_is_busy(settings) -> None:
    sock = _BusySock({**HELLO_BASE, "worker_id": str(uuid.uuid4())})
    task, *_rest = await _start(settings, _Execution(), sock)
    try:
        await _wait_for(lambda: len(_sent_types(sock, "heartbeat")) >= 4)
    finally:
        task.cancel()


async def test_inventory_runs_on_a_timer_when_the_socket_is_busy(
    settings, monkeypatch
) -> None:
    monkeypatch.setattr("apipi.worker.hub.INVENTORY_INTERVAL", 0.05)
    sock = _BusySock({**HELLO_BASE, "worker_id": str(uuid.uuid4())})
    task, *_rest = await _start(settings, _Execution(), sock)
    try:
        await _wait_for(lambda: len(_sent_types(sock, "inventory")) >= 2)
    finally:
        task.cancel()


async def test_drain_runs_when_the_socket_is_busy(settings) -> None:
    from apipi.worker.hub import _serve_connection
    from apipi.worker.outbox import Outbox

    sock = _BusySock({**HELLO_BASE, "worker_id": str(uuid.uuid4())})
    draining = asyncio.Event()
    server = asyncio.create_task(
        _serve_connection(
            settings,
            _Execution(),
            Outbox(),
            _Relay(),
            sock,  # type: ignore[arg-type]
            {},
            set(),
            set(),
            draining,
            None,
            30.0,
            None,
            CommandDedupe(),
        )
    )
    await asyncio.sleep(0.05)
    draining.set()
    outcome, _deadline = await asyncio.wait_for(server, timeout=5)
    assert outcome == "drained"
    assert any(
        message.get("drain") is True for message in _sent_types(sock, "heartbeat")
    )


async def test_hello_without_heartbeat_fields_is_rejected(settings) -> None:
    from apipi.config import ConfigError

    sock = _Sock([{"ok": True, "worker_id": str(uuid.uuid4())}])
    task, *_rest = await _start(settings, _Execution(), sock)
    with pytest.raises(ConfigError, match="heartbeat_seconds"):
        await asyncio.wait_for(task, timeout=5)


async def test_worker_heartbeat_gap_metric_and_late_warning(
    settings, caplog: pytest.LogCaptureFixture
) -> None:
    from apipi.gateway.metrics import Metrics

    metrics = Metrics()
    execution = _Execution()
    execution.metrics = metrics
    hello = {
        **HELLO_BASE,
        "lease_ttl_seconds": 0.06,
        "heartbeat_seconds": 0.05,
        "worker_id": str(uuid.uuid4()),
    }
    sock = _BusySock(hello)
    with caplog.at_level("WARNING", logger="apipi.worker"):
        task, *_rest = await _start(settings, execution, sock)
        try:
            await _wait_for(lambda: len(_sent_types(sock, "heartbeat")) >= 2)
        finally:
            task.cancel()
    body = metrics.scrape().decode()
    count = [
        line
        for line in body.splitlines()
        if line.startswith("apipi_worker_heartbeat_gap_seconds_count")
    ]
    assert count and float(count[0].split()[-1]) >= 2
    late = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker.heartbeat.late"
    ]
    assert late
    assert late[0].__dict__["source"] == "worker"


async def test_command_cursor_continues_the_sequence_and_never_goes_back(
    settings,
) -> None:
    session_id = uuid.uuid4()
    first = _command(session_id, uuid.uuid4(), str(uuid.uuid4()))
    first["payload"]["last_seq"] = 40
    older = _command(session_id, uuid.uuid4(), first["lease_id"])
    older["payload"]["last_seq"] = 10
    hello = {**HELLO_BASE, "worker_id": str(uuid.uuid4()), "sessions": {}}
    sock = _Sock([hello, first])
    execution = _Execution()
    task, outbox, _leases, _tasks = await _start(settings, execution, sock)
    try:
        await _wait_for(lambda: len(execution.turns) == 1)
        assert outbox.high_water(session_id) == 40
        assert outbox.append(session_id, "turn.status", {})["seq"] == 41
        sock._incoming.append(older)
        await _wait_for(lambda: len(execution.turns) == 2)
        assert outbox.high_water(session_id) == 41
    finally:
        task.cancel()


async def test_command_without_cursor_is_logged(
    settings, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = uuid.uuid4()
    command = _command(session_id, uuid.uuid4(), str(uuid.uuid4()))
    del command["payload"]["last_seq"]
    sock = _Sock([{**HELLO_BASE, "worker_id": str(uuid.uuid4())}, command])
    execution = _Execution()
    with caplog.at_level("WARNING", logger="apipi.worker"):
        task, *_rest = await _start(settings, execution, sock)
        try:
            await _wait_for(lambda: len(execution.turns) == 1)
        finally:
            task.cancel()
    assert any(
        getattr(record, "event", None) == "worker.command.cursor_missing"
        for record in caplog.records
    )


async def _released(settings, execution, session_id):
    lease_id = str(uuid.uuid4())
    hello = {
        **HELLO_BASE,
        "worker_id": str(uuid.uuid4()),
        "sessions": {str(session_id): 0},
    }
    sock = _Sock([hello])
    task, outbox, _leases, _tasks = await _start(
        settings, execution, sock, session_leases={session_id: lease_id}
    )
    await _wait_for(lambda: execution.note_stopped is not None)
    return task, outbox, sock, lease_id


async def test_release_waits_until_the_outbox_is_acked(settings) -> None:
    session_id = uuid.uuid4()
    execution = _Execution()
    task, outbox, sock, lease_id = await _released(settings, execution, session_id)
    try:
        sock._incoming.clear()
        envelope = outbox.append(session_id, "lifecycle.stop", {"reason": "idle"})
        releasing = asyncio.create_task(execution.note_stopped(session_id))
        await _wait_for(lambda: any(m.get("seq") == envelope["seq"] for m in sock.sent))
        await asyncio.sleep(0.1)
        assert not releasing.done()
        assert _sent_types(sock, "lease.release") == []
        sock._incoming.append(
            {"type": "ack", "session_id": str(session_id), "last_seq": envelope["seq"]}
        )
        await asyncio.wait_for(releasing, timeout=5)
        released = _sent_types(sock, "lease.release")
        assert [m["lease_id"] for m in released] == [lease_id]
        sent_kinds = [m.get("type") for m in sock.sent]
        assert sent_kinds.index("lifecycle.stop") < sent_kinds.index("lease.release")
    finally:
        task.cancel()


async def test_release_goes_out_after_the_timeout_and_is_logged(
    settings, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("apipi.worker.hub.RELEASE_FLUSH_TIMEOUT", 0.1)
    session_id = uuid.uuid4()
    execution = _Execution()
    task, outbox, sock, lease_id = await _released(settings, execution, session_id)
    try:
        outbox.append(session_id, "lifecycle.stop", {"reason": "idle"})
        with caplog.at_level("WARNING", logger="apipi.worker"):
            await asyncio.wait_for(execution.note_stopped(session_id), timeout=5)
        assert [m["lease_id"] for m in _sent_types(sock, "lease.release")] == [lease_id]
        assert any(
            getattr(record, "event", None) == "worker.release.unflushed"
            for record in caplog.records
        )
    finally:
        task.cancel()


async def test_session_stop_acks_after_session_stopped_is_acked(settings) -> None:
    from apipi.worker.outbox import Outbox

    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    outbox = Outbox()

    class _StopExecution(_Execution):
        def __init__(self) -> None:
            super().__init__()
            self.settings = settings
            self.outbox = outbox

        async def teardown(self, session_id: uuid.UUID) -> None:
            outbox.append(session_id, "lifecycle.stop", {"reason": "stop"})
            if self.note_stopped is not None:
                await self.note_stopped(session_id)

    stop = _command(session_id, uuid.uuid4(), lease_id, op="session.stop")
    hello = {**HELLO_BASE, "worker_id": str(uuid.uuid4()), "sessions": {}}
    sock = _Sock([hello, stop])
    execution = _StopExecution()
    task, _outbox, leases, _tasks = await _start(
        settings, execution, sock, outbox=outbox
    )
    try:
        await _wait_for(lambda: outbox.high_water(session_id) == 2)
        await asyncio.sleep(0.1)
        assert _sent_types(sock, "lease.ack") == []
        assert _sent_types(sock, "lease.release") == []
        assert [e["type"] for e in outbox.pending(session_id)] == [
            "lifecycle.stop",
            "session.stopped",
        ]
        sock._incoming.append(
            {"type": "ack", "session_id": str(session_id), "last_seq": 2}
        )
        await _wait_for(lambda: len(_sent_types(sock, "lease.ack")) == 1)
        assert _sent_types(sock, "lease.release") == []
        assert session_id not in leases
    finally:
        task.cancel()
