"""Worker side robustness of the socket (#487)."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import websockets
from websockets.frames import Close

from apipi.common.metrics import Metrics
from apipi.config import ConfigError, Settings
from apipi.worker.artifact_upload import (
    PresignDisconnected,
    PresignFuture,
    fail_lost_presign_waiters,
    upload_via_presign,
)
from apipi.worker.client import (
    BadHello,
    HelloTimeout,
    _reconnect_reason,
    _serve_connection,
    reconnect_delay,
    run_worker,
)
from apipi.worker.commands import CommandDedupe
from apipi.worker.deltas import DeltaRelay
from apipi.worker.outbox import Outbox


def _hello(**extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "worker_id": str(uuid.uuid4()),
        "lease_ttl_seconds": 30,
        "heartbeat_seconds": 0.05,
        "sessions": {},
        **extra,
    }


class _Sock:
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

    async def kill_unheld(self, reason: str = "") -> None:
        del reason

    async def close(self) -> None:
        self.closed = True


class _Execution:
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


def _command(
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
class _Run:
    task: "asyncio.Task[tuple[str, float | None]]"
    sock: _Sock
    execution: _Execution
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


def _start(
    settings: Settings,
    sock: _Sock,
    *,
    execution: _Execution | None = None,
    outbox: Outbox | None = None,
    leases: dict[uuid.UUID, str] | None = None,
    pending: dict[uuid.UUID, str] | None = None,
    draining: asyncio.Event | None = None,
    wait: float = 30.0,
) -> _Run:
    outbox = outbox if outbox is not None else Outbox()
    execution = execution if execution is not None else _Execution(settings, outbox)
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
    return _Run(task, sock, execution, outbox, leases, dedupe, draining, pending, tasks)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


async def test_stop_that_needs_a_presign_keeps_heartbeats_and_replies_flowing(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())

    async def harvest(sid: uuid.UUID) -> None:
        await upload_via_presign(
            outbox,
            execution.presign_waiters,
            settings,
            sid,
            kind="artifact",
            filename="a.txt",
            content_type=None,
            data=b"x",
            timeout=5.0,
        )

    execution.on_teardown = harvest
    sock = _Sock([_hello(), _command(session_id, lease_id, "session.stop")])
    run = _start(settings, sock, execution=execution, outbox=outbox)
    try:
        await _wait_for(lambda: sock.of("artifact.presign"))
        beats = len(sock.of("heartbeat"))
        await _wait_for(lambda: len(sock.of("heartbeat")) >= beats + 3)
        assert sock.of("lease.ack") == []
        request_id = sock.of("artifact.presign")[0]["payload"]["request_id"]
        sock.push(
            {
                "type": "artifact.presign.reply",
                "session_id": str(session_id),
                "request_id": request_id,
                "ok": True,
                "unchanged": True,
            }
        )
        await _wait_for(lambda: outbox.high_water(session_id) >= 2)
        sock.push(
            {
                "type": "ack",
                "session_id": str(session_id),
                "last_seq": outbox.high_water(session_id),
            }
        )
        await _wait_for(lambda: sock.of("lease.ack"))
        assert session_id not in run.leases
        assert len(run.dedupe) == 0
        assert outbox.describe()["sessions"] == 0
    finally:
        await run.stop()


async def test_revoke_teardown_runs_as_a_task_and_orders_commands_per_session(
    settings: Settings,
) -> None:
    gate = asyncio.Event()
    outbox = Outbox()
    execution = _Execution(settings, outbox)

    async def slow(_sid: uuid.UUID) -> None:
        await gate.wait()

    execution.on_teardown = slow
    revoked = uuid.uuid4()
    other = uuid.uuid4()
    old_lease = str(uuid.uuid4())
    sock = _Sock([_hello()])
    run = _start(
        settings, sock, execution=execution, outbox=outbox, leases={revoked: old_lease}
    )
    try:
        await _wait_for(lambda: sock.of("heartbeat"))
        sock.push(
            {"type": "lease.revoke", "session_id": str(revoked), "lease_id": old_lease}
        )
        sock.push(_command(other, str(uuid.uuid4())))
        await _wait_for(lambda: len(sock.of("lease.ack")) == 1)
        assert revoked not in run.leases
        again = _command(revoked, str(uuid.uuid4()))
        sock.push(again)
        await asyncio.sleep(0.1)
        assert len(sock.of("lease.ack")) == 1
        assert execution.turns == [other]
        gate.set()
        await _wait_for(lambda: len(sock.of("lease.ack")) == 2)
        await _wait_for(lambda: revoked in execution.turns)
    finally:
        await run.stop()


async def test_inventory_revokes_run_as_tasks(settings: Settings) -> None:
    gate = asyncio.Event()
    outbox = Outbox()
    execution = _Execution(settings, outbox)

    async def slow(_sid: uuid.UUID) -> None:
        await gate.wait()

    execution.on_teardown = slow
    revoked = uuid.uuid4()
    lease = str(uuid.uuid4())
    sock = _Sock([_hello()])
    run = _start(
        settings, sock, execution=execution, outbox=outbox, leases={revoked: lease}
    )
    try:
        await _wait_for(lambda: sock.of("heartbeat"))
        sock.push(
            {
                "type": "inventory.reply",
                "revoke": [{"session_id": str(revoked), "lease_id": lease}],
            }
        )
        await _wait_for(lambda: execution.teardowns == [revoked])
        beats = len(sock.of("heartbeat"))
        await _wait_for(lambda: len(sock.of("heartbeat")) >= beats + 2)
        assert revoked not in run.leases
        gate.set()
    finally:
        await run.stop()


async def test_malformed_frames_are_counted_logged_and_skipped(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    metrics = Metrics()
    execution.metrics = metrics
    sock = _Sock([_hello()])
    run = _start(settings, sock, execution=execution, outbox=outbox)
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    try:
        await _wait_for(lambda: sock.of("heartbeat"))
        sock.push("{not json")
        sock.push(b"\xff\xfe")
        sock.push("[1, 2]")
        sock.push(_command(uuid.uuid4(), str(uuid.uuid4())))
        await _wait_for(lambda: sock.of("lease.ack"))
        assert not run.task.done()
        body = metrics.scrape().decode()
        line = [
            row
            for row in body.splitlines()
            if row.startswith("apipi_worker_messages_total")
            and 'type="unknown"' in row
            and 'direction="in"' in row
        ]
        assert line and line[0].endswith(" 3.0")
        invalid = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "worker.message.invalid"
        ]
        assert len(invalid) == 1
    finally:
        await run.stop()


async def test_failed_command_is_answered_with_an_error_and_not_deduped(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    calls = 0

    async def boom(_sid: uuid.UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cancel failed")

    execution.on_cancel = boom
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    command = _command(session_id, lease, "turn.cancel")
    sock = _Sock([_hello()])
    run = _start(
        settings, sock, execution=execution, outbox=outbox, leases={session_id: lease}
    )
    caplog.set_level(logging.ERROR)
    try:
        await _wait_for(lambda: sock.of("heartbeat"))
        sock.push(command)
        await _wait_for(lambda: outbox.pending(session_id))
        error = outbox.pending(session_id)[0]
        assert error["type"] == "error"
        assert error["payload"]["code"] == "internal"
        assert any(
            getattr(r, "event", None) == "background.task.failed"
            for r in caplog.records
        )
        sock.push(dict(command))
        await _wait_for(lambda: calls == 2)
    finally:
        await run.stop()


async def test_failed_stop_is_not_acked_and_runs_again_on_retransmit(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    calls = 0

    async def flaky(_sid: uuid.UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("teardown failed")

    execution.on_teardown = flaky
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    stop = _command(session_id, lease, "session.stop")
    sock = _Sock([_hello(), stop])
    run = _start(settings, sock, execution=execution, outbox=outbox)
    try:
        await _wait_for(lambda: calls == 1)
        await _wait_for(lambda: outbox.pending(session_id))
        assert outbox.pending(session_id)[0]["type"] == "error"
        assert sock.of("lease.ack") == []
        sock.push(dict(stop))
        await _wait_for(lambda: calls == 2)
        await _wait_for(
            lambda: (
                outbox.high_water(session_id) >= 2
                and outbox.pending(session_id)[-1]["type"] == "session.stopped"
            )
        )
        sock.push(
            {
                "type": "ack",
                "session_id": str(session_id),
                "last_seq": outbox.high_water(session_id),
            }
        )
        await _wait_for(lambda: sock.of("lease.ack"))
    finally:
        await run.stop()


async def test_leases_are_tracked_only_for_accepted_commands(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    cancelled = uuid.uuid4()
    rejected = uuid.uuid4()
    rejected_lease = str(uuid.uuid4())
    sock = _Sock(
        [
            _hello(),
            _command(cancelled, str(uuid.uuid4()), "turn.cancel"),
            _command(rejected, rejected_lease, run_mode="microvm"),
        ]
    )
    run = _start(settings, sock, execution=execution, outbox=outbox)
    try:
        await _wait_for(lambda: len(sock.of("lease.ack")) == 2)
        await _wait_for(lambda: sock.of("lease.release"))
        assert sock.of("lease.release")[0]["lease_id"] == rejected_lease
        assert run.leases == {}
        assert execution.turns == []
    finally:
        await run.stop()


async def test_first_frame_that_is_not_hello_reconnects(settings: Settings) -> None:
    for frame in (
        {"ok": False, "error": "database busy"},
        {"type": "ack"},
        "not json",
        "[1]",
    ):
        run = _start(settings, _Sock([frame]))
        with pytest.raises(BadHello):
            await asyncio.wait_for(run.task, timeout=5)
        assert _reconnect_reason(BadHello()) == "error"


async def test_rejections_stop_the_worker_with_a_clear_message(
    settings: Settings,
) -> None:
    for error in ("unauthorized", "revoked", "unsupported_protocol", "token_bound"):
        run = _start(settings, _Sock([{"ok": False, "error": error}]))
        with pytest.raises(ConfigError, match=error):
            await asyncio.wait_for(run.task, timeout=5)


async def test_hello_has_a_timeout(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.client.HELLO_TIMEOUT", 0.05)
    run = _start(settings, _Sock())
    with pytest.raises(HelloTimeout) as caught:
        await asyncio.wait_for(run.task, timeout=5)
    assert _reconnect_reason(caught.value) == "hello_timeout"


def test_reconnect_reasons() -> None:
    ping = websockets.ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))
    assert _reconnect_reason(ping) == "ping_timeout"
    assert _reconnect_reason(websockets.ConnectionClosedError(None, None)) == "closed"
    assert _reconnect_reason(OSError()) == "connect_error"
    assert _reconnect_reason(RuntimeError()) == "error"


def test_reconnect_delay_is_exponential_with_full_jitter_and_a_cap() -> None:
    ceilings = [reconnect_delay(n, rng=lambda: 1.0) for n in range(1, 9)]
    assert ceilings == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
    assert reconnect_delay(3, rng=lambda: 0.0) == 0.0
    assert reconnect_delay(3, rng=lambda: 0.5) == 1.0
    assert reconnect_delay(500, rng=lambda: 1.0) == 10.0
    samples = {reconnect_delay(4) for _ in range(20)}
    assert len(samples) > 1
    assert all(0 <= value <= 4.0 for value in samples)


async def test_reconnect_sends_each_envelope_once_and_counts_the_resend(
    settings: Settings,
) -> None:
    metrics = Metrics()
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    execution.metrics = metrics
    outbox.metrics = metrics
    session_id = uuid.uuid4()
    leases = {session_id: str(uuid.uuid4())}
    for n in range(3):
        outbox.append(session_id, "event", {"n": n})
    first = _Sock([_hello(sessions={str(session_id): 0})])
    run = _start(settings, first, execution=execution, outbox=outbox, leases=leases)
    try:
        await _wait_for(lambda: len(first.of("event")) == 3)
        outbox.append(session_id, "event", {"n": 3})
        await _wait_for(lambda: len(first.of("event")) == 4)
        await asyncio.sleep(0.1)
        assert [m["seq"] for m in first.of("event")] == [1, 2, 3, 4]
    finally:
        await run.stop()
    second = _Sock([_hello(sessions={str(session_id): 1})])
    run = _start(settings, second, execution=execution, outbox=outbox, leases=leases)
    try:
        await _wait_for(lambda: len(second.of("event")) == 3)
        outbox.append(session_id, "event", {"n": 4})
        await _wait_for(lambda: len(second.of("event")) == 4)
        await asyncio.sleep(0.1)
        assert [m["seq"] for m in second.of("event")] == [2, 3, 4, 5]
        body = metrics.scrape().decode()
        assert "apipi_worker_replayed_total 3.0" in body
    finally:
        await run.stop()


async def test_spooled_envelopes_without_a_claim_are_sent_after_a_restart(
    settings: Settings, tmp_path: Path
) -> None:
    session_id = uuid.uuid4()
    first = Outbox(spool_dir=tmp_path / "spool")
    first.append(session_id, "turn.status", {"status": "completed"})
    first.append(session_id, "usage", {"input": 1})
    del first
    restarted = Outbox(spool_dir=tmp_path / "spool")
    restarted.load_spool()
    sock = _Sock([_hello()])
    run = _start(settings, sock, outbox=restarted)
    try:
        await _wait_for(lambda: len(sock.of("turn.status")) == 1)
        await _wait_for(lambda: len(sock.of("usage")) == 1)
        sock.push({"type": "ack", "session_id": str(session_id), "last_seq": 2})
        await _wait_for(lambda: restarted.pending_sessions() == [])
        assert not (tmp_path / "spool" / f"{session_id}.jsonl").exists()
    finally:
        await run.stop()


async def test_lease_released_while_offline_is_released_after_the_next_hello(
    settings: Settings,
) -> None:
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    pending = {session_id: lease}
    sock = _Sock([_hello()])
    run = _start(settings, sock, pending=pending)
    try:
        await _wait_for(lambda: sock.of("lease.release"))
        assert sock.of("lease.release")[0]["lease_id"] == lease
        assert pending == {}
        assert run.leases == {}
    finally:
        await run.stop()


async def test_drain_waits_for_the_outbox_to_be_acked(settings: Settings) -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    outbox.append(session_id, "usage", {"input": 1})
    draining = asyncio.Event()
    draining.set()
    sock = _Sock([_hello()])
    run = _start(settings, sock, outbox=outbox, draining=draining)
    try:
        await asyncio.sleep(0.3)
        assert not run.task.done()
        assert any(m.get("drain") for m in sock.of("heartbeat"))
        sock.push({"type": "ack", "session_id": str(session_id), "last_seq": 1})
        outcome, _deadline = await asyncio.wait_for(run.task, timeout=5)
        assert outcome == "drained"
    finally:
        await run.stop()


async def test_drain_gives_up_on_the_outbox_at_the_timeout(settings: Settings) -> None:
    outbox = Outbox()
    outbox.append(uuid.uuid4(), "usage", {"input": 1})
    draining = asyncio.Event()
    draining.set()
    run = _start(
        settings, _Sock([_hello()]), outbox=outbox, draining=draining, wait=0.2
    )
    outcome, _deadline = await asyncio.wait_for(run.task, timeout=5)
    assert outcome == "drain_timeout"
    assert run.execution.pool.closed


async def test_disconnect_fails_only_the_presign_waiters_whose_reply_was_lost() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    loop = asyncio.get_running_loop()
    acked = PresignFuture(session_id, loop=loop)
    acked.seq = outbox.append(session_id, "artifact.presign", {"x": 1})["seq"]
    waiting = PresignFuture(session_id, loop=loop)
    outbox.acked(session_id, acked.seq)
    waiting.seq = outbox.append(session_id, "artifact.presign", {"x": 2})["seq"]
    waiters: dict[uuid.UUID, Any] = {uuid.uuid4(): acked, uuid.uuid4(): waiting}
    assert fail_lost_presign_waiters(waiters, outbox) == 1
    with pytest.raises(PresignDisconnected):
        acked.result()
    assert not waiting.done()


async def test_cancelled_upload_passes_cancelled_error_through(
    settings: Settings,
) -> None:
    outbox = Outbox()
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
    task = asyncio.create_task(
        upload_via_presign(
            outbox,
            waiters,
            settings,
            uuid.uuid4(),
            kind="artifact",
            filename="a",
            content_type=None,
            data=b"x",
            timeout=30.0,
        )
    )
    await _wait_for(lambda: waiters)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiters == {}


async def test_lost_connection_fails_waiting_uploads_fast(settings: Settings) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    metrics = Metrics()
    execution.metrics = metrics
    outbox.metrics = metrics
    session_id = uuid.uuid4()
    sock = _Sock([_hello(sessions={str(session_id): 0})])
    run = _start(
        settings,
        sock,
        execution=execution,
        outbox=outbox,
        leases={session_id: str(uuid.uuid4())},
    )
    upload = asyncio.create_task(
        upload_via_presign(
            outbox,
            execution.presign_waiters,
            settings,
            session_id,
            kind="artifact",
            filename="a",
            content_type=None,
            data=b"x",
            timeout=30.0,
        )
    )
    try:
        await _wait_for(lambda: sock.of("artifact.presign"))
        sock.push({"type": "ack", "session_id": str(session_id), "last_seq": 1})
        await _wait_for(lambda: outbox.acked_seq(session_id) == 1)
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)
        with pytest.raises(PresignDisconnected):
            await asyncio.wait_for(upload, timeout=2)
        body = metrics.scrape().decode()
        assert (
            'apipi_worker_waiter_total{kind="presign",result="disconnected"} 1.0'
            in body
        )
    finally:
        await run.stop()


async def test_oversize_delta_is_dropped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apipi.worker.deltas.MAX_MESSAGE_BYTES", 300)
    metrics = Metrics()
    sent: list[dict[str, Any]] = []

    async def send(envelope: dict[str, Any]) -> None:
        sent.append(envelope)

    relay = DeltaRelay(send, window=0, metrics=metrics)
    session_id, turn_id = uuid.uuid4(), uuid.uuid4()
    await relay.submit(session_id, turn_id, "x" * 1000)
    await relay.flush()
    await relay.submit(session_id, turn_id, "ok")
    await relay.flush()
    assert [e["payload"]["text"] for e in sent] == ["ok"]
    assert 'apipi_worker_deltas_dropped_total{reason="oversize"} 1.0' in (
        metrics.scrape().decode()
    )


@dataclass
class _Connects:
    sockets: list[Any]
    calls: int = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_Connect":
        self.calls += 1
        item = self.sockets.pop(0) if self.sockets else None
        return _Connect(item)


class _Connect:
    def __init__(self, item: Any) -> None:
        self.item = item

    async def __aenter__(self) -> Any:
        if self.item is None:
            raise OSError("down")
        return self.item

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _RunExecution(_Execution):
    async def _idle(self) -> None:
        await asyncio.Event().wait()

    reap_loop = observe_loop = reap_workspace_loop = sandbox_seen_loop = _idle

    async def close(self) -> None:
        return None


def _patch_run_worker(
    monkeypatch: pytest.MonkeyPatch,
    execution: _RunExecution,
    *,
    metrics: Metrics | None = None,
) -> None:
    monkeypatch.setattr(
        "apipi.worker.execution.local_execution", lambda *_a, **_k: execution
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability", lambda _s: (metrics, None)
    )
    monkeypatch.setattr("apipi.worker.client._install_drain_signals", lambda _e: None)
    monkeypatch.setattr("apipi.worker.client.random.random", lambda: 0.0)


def _worker_settings(tmp_path: Path, **extra: Any) -> Settings:
    (tmp_path / "worker.token").write_text("secret\n")
    return Settings(
        run_mode="none", worker_token_file=str(tmp_path / "worker.token"), **extra
    )


async def test_bad_first_frame_reconnects_instead_of_exiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _worker_settings(tmp_path)
    metrics = Metrics()
    execution = _RunExecution(settings, Outbox())
    _patch_run_worker(monkeypatch, execution, metrics=metrics)

    async def no_scrape(*_a: object, **_k: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("apipi.worker.scrape.serve_metrics", no_scrape)
    connects = _Connects(
        [_Sock([{"ok": False, "error": "database busy"}]), _Sock([_hello()]), None]
    )
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connects)
    )
    try:
        await _wait_for(lambda: connects.calls >= 2)
        await _wait_for(
            lambda: (
                'apipi_worker_reconnects_total{reason="error"} 1.0'
                in metrics.scrape().decode()
            )
        )
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_tls_context_is_rebuilt_on_every_reconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _worker_settings(tmp_path)
    execution = _RunExecution(settings, Outbox())
    _patch_run_worker(monkeypatch, execution)
    built = 0

    def kwargs(_settings: Settings, _url: str) -> dict[str, Any]:
        nonlocal built
        built += 1
        return {}

    monkeypatch.setattr("apipi.worker.client._worker_connect_kwargs", kwargs)
    connects = _Connects([None, None, None, _Sock([_hello()])])
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connects)
    )
    try:
        await _wait_for(lambda: connects.calls >= 4)
        assert built == connects.calls
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_drain_timeout_is_enforced_while_disconnected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = tmp_path / "spool"
    session_id = uuid.uuid4()
    seed = Outbox(spool_dir=spool)
    seed.append(session_id, "usage", {"input": 1})
    del seed
    settings = _worker_settings(tmp_path, worker_outbox_dir=str(spool))
    execution = _RunExecution(settings, Outbox())
    _patch_run_worker(monkeypatch, execution)
    monkeypatch.setattr(
        "apipi.worker.client._install_drain_signals", lambda event: event.set()
    )
    caplog.set_level(logging.INFO, logger="apipi.worker")
    connects = _Connects([])
    status = await asyncio.wait_for(
        run_worker(
            settings,
            url="http://127.0.0.1:8000",
            connect=connects,
            drain_timeout=0.3,
        ),
        timeout=10,
    )
    assert status == 1
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "worker.spool.recovered" in events
    assert "worker.drain.started" in events
    assert "worker.drain.finished" in events


async def test_drain_while_disconnected_exits_when_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _worker_settings(tmp_path)
    execution = _RunExecution(settings, Outbox())
    _patch_run_worker(monkeypatch, execution)
    monkeypatch.setattr(
        "apipi.worker.client._install_drain_signals", lambda event: event.set()
    )
    status = await asyncio.wait_for(
        run_worker(
            settings,
            url="http://127.0.0.1:8000",
            connect=_Connects([]),
            drain_timeout=5,
        ),
        timeout=10,
    )
    assert status == 0


async def test_observe_loop_survives_a_failing_round(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from apipi.worker import execution as execution_module
    from apipi.worker.execution import local_execution

    real = execution_module.run_loop

    async def fast(*args: Any, **kwargs: Any) -> None:
        kwargs["interval"] = 0.01
        await real(*args, **kwargs)

    monkeypatch.setattr(execution_module, "run_loop", fast)
    execution = local_execution(settings, outbox=Outbox(), metrics=Metrics())
    calls = 0

    async def sweep() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("sweep failed")

    monkeypatch.setattr(execution.pool, "sweep_dead", sweep)
    task = asyncio.create_task(execution.observe_loop())
    try:
        await _wait_for(lambda: calls >= 3)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
