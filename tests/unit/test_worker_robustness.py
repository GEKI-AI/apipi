"""Worker side robustness of the socket (#487)."""

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import websockets
from tests.support.waits import closed_on_cancel, until
from tests.support.worker_connection import (
    FakeExecution,
    FakeSock,
    command_frame,
    hello_frame,
    start_connection,
)
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
    reconnect_delay,
    run_worker,
)
from apipi.worker.deltas import DeltaRelay
from apipi.worker.outbox import Outbox


async def test_stop_that_needs_a_presign_keeps_heartbeats_and_replies_flowing(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
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
    sock = FakeSock(
        [hello_frame(), command_frame(session_id, lease_id, "session.stop")]
    )
    run = start_connection(settings, sock, execution=execution, outbox=outbox)
    try:
        await until(lambda: sock.of("artifact.presign"))
        beats = len(sock.of("heartbeat"))
        await until(lambda: len(sock.of("heartbeat")) >= beats + 3)
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
        await until(lambda: outbox.high_water(session_id) >= 2)
        sock.push(
            {
                "type": "ack",
                "session_id": str(session_id),
                "last_seq": outbox.high_water(session_id),
            }
        )
        await until(lambda: sock.of("lease.ack"))
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
    execution = FakeExecution(settings, outbox)

    async def slow(_sid: uuid.UUID) -> None:
        await gate.wait()

    execution.on_teardown = slow
    revoked = uuid.uuid4()
    other = uuid.uuid4()
    old_lease = str(uuid.uuid4())
    sock = FakeSock([hello_frame()])
    run = start_connection(
        settings, sock, execution=execution, outbox=outbox, leases={revoked: old_lease}
    )
    try:
        await until(lambda: sock.of("heartbeat"))
        sock.push(
            {"type": "lease.revoke", "session_id": str(revoked), "lease_id": old_lease}
        )
        sock.push(command_frame(other, str(uuid.uuid4())))
        await until(lambda: len(sock.of("lease.ack")) == 1)
        assert revoked not in run.leases
        again = command_frame(revoked, str(uuid.uuid4()))
        sock.push(again)
        await asyncio.sleep(0.1)
        assert len(sock.of("lease.ack")) == 1
        assert execution.turns == [other]
        gate.set()
        await until(lambda: len(sock.of("lease.ack")) == 2)
        await until(lambda: revoked in execution.turns)
    finally:
        await run.stop()


async def test_inventory_revokes_run_as_tasks(settings: Settings) -> None:
    gate = asyncio.Event()
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)

    async def slow(_sid: uuid.UUID) -> None:
        await gate.wait()

    execution.on_teardown = slow
    revoked = uuid.uuid4()
    lease = str(uuid.uuid4())
    sock = FakeSock([hello_frame()])
    run = start_connection(
        settings, sock, execution=execution, outbox=outbox, leases={revoked: lease}
    )
    try:
        await until(lambda: sock.of("heartbeat"))
        sock.push(
            {
                "type": "inventory.reply",
                "revoke": [{"session_id": str(revoked), "lease_id": lease}],
            }
        )
        await until(lambda: execution.teardowns == [revoked])
        beats = len(sock.of("heartbeat"))
        await until(lambda: len(sock.of("heartbeat")) >= beats + 2)
        assert revoked not in run.leases
        gate.set()
    finally:
        await run.stop()


async def test_malformed_frames_are_counted_logged_and_skipped(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
    metrics = Metrics()
    execution.metrics = metrics
    sock = FakeSock([hello_frame()])
    run = start_connection(settings, sock, execution=execution, outbox=outbox)
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    try:
        await until(lambda: sock.of("heartbeat"))
        sock.push("{not json")
        sock.push(b"\xff\xfe")
        sock.push("[1, 2]")
        sock.push(command_frame(uuid.uuid4(), str(uuid.uuid4())))
        await until(lambda: sock.of("lease.ack"))
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
    execution = FakeExecution(settings, outbox)
    calls = 0

    async def boom(_sid: uuid.UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cancel failed")

    execution.on_cancel = boom
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    command = command_frame(session_id, lease, "turn.cancel")
    sock = FakeSock([hello_frame()])
    run = start_connection(
        settings, sock, execution=execution, outbox=outbox, leases={session_id: lease}
    )
    caplog.set_level(logging.ERROR)
    try:
        await until(lambda: sock.of("heartbeat"))
        sock.push(command)
        await until(lambda: outbox.pending(session_id))
        error = outbox.pending(session_id)[0]
        assert error["type"] == "error"
        assert error["payload"]["code"] == "internal"
        assert any(
            getattr(r, "event", None) == "background.task.failed"
            for r in caplog.records
        )
        sock.push(dict(command))
        await until(lambda: calls == 2)
    finally:
        await run.stop()


async def test_failed_stop_is_not_acked_and_runs_again_on_retransmit(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
    calls = 0

    async def flaky(_sid: uuid.UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("teardown failed")

    execution.on_teardown = flaky
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    stop = command_frame(session_id, lease, "session.stop")
    sock = FakeSock([hello_frame(), stop])
    run = start_connection(settings, sock, execution=execution, outbox=outbox)
    try:
        await until(lambda: calls == 1)
        await until(lambda: outbox.pending(session_id))
        assert outbox.pending(session_id)[0]["type"] == "error"
        assert sock.of("lease.ack") == []
        sock.push(dict(stop))
        await until(lambda: calls == 2)
        await until(
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
        await until(lambda: sock.of("lease.ack"))
    finally:
        await run.stop()


async def test_leases_are_tracked_only_for_accepted_commands(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
    cancelled = uuid.uuid4()
    rejected = uuid.uuid4()
    rejected_lease = str(uuid.uuid4())
    sock = FakeSock(
        [
            hello_frame(),
            command_frame(cancelled, str(uuid.uuid4()), "turn.cancel"),
            command_frame(rejected, rejected_lease, run_mode="microvm"),
        ]
    )
    run = start_connection(settings, sock, execution=execution, outbox=outbox)
    try:
        await until(lambda: len(sock.of("lease.ack")) == 2)
        await until(lambda: sock.of("lease.release"))
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
        run = start_connection(settings, FakeSock([frame]))
        with pytest.raises(BadHello):
            await asyncio.wait_for(run.task, timeout=5)
        assert _reconnect_reason(BadHello()) == "error"


async def test_rejections_stop_the_worker_with_a_clear_message(
    settings: Settings,
) -> None:
    for error in ("unauthorized", "revoked", "unsupported_protocol", "token_bound"):
        run = start_connection(settings, FakeSock([{"ok": False, "error": error}]))
        with pytest.raises(ConfigError, match=error):
            await asyncio.wait_for(run.task, timeout=5)


async def test_hello_has_a_timeout(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.client.HELLO_TIMEOUT", 0.05)
    run = start_connection(settings, FakeSock())
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
    execution = FakeExecution(settings, outbox)
    execution.metrics = metrics
    outbox.metrics = metrics
    session_id = uuid.uuid4()
    leases = {session_id: str(uuid.uuid4())}
    for n in range(3):
        outbox.append(session_id, "event", {"n": n})
    first = FakeSock([hello_frame(sessions={str(session_id): 0})])
    run = start_connection(
        settings, first, execution=execution, outbox=outbox, leases=leases
    )
    try:
        await until(lambda: len(first.of("event")) == 3)
        outbox.append(session_id, "event", {"n": 3})
        await until(lambda: len(first.of("event")) == 4)
        await asyncio.sleep(0.1)
        assert [m["seq"] for m in first.of("event")] == [1, 2, 3, 4]
    finally:
        await run.stop()
    second = FakeSock([hello_frame(sessions={str(session_id): 1})])
    run = start_connection(
        settings, second, execution=execution, outbox=outbox, leases=leases
    )
    try:
        await until(lambda: len(second.of("event")) == 3)
        outbox.append(session_id, "event", {"n": 4})
        await until(lambda: len(second.of("event")) == 4)
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
    sock = FakeSock([hello_frame()])
    run = start_connection(settings, sock, outbox=restarted)
    try:
        await until(lambda: len(sock.of("turn.status")) == 1)
        await until(lambda: len(sock.of("usage")) == 1)
        sock.push({"type": "ack", "session_id": str(session_id), "last_seq": 2})
        await until(lambda: restarted.pending_sessions() == [])
        assert not (tmp_path / "spool" / f"{session_id}.jsonl").exists()
    finally:
        await run.stop()


async def test_lease_released_while_offline_is_released_after_the_next_hello(
    settings: Settings,
) -> None:
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    pending = {session_id: lease}
    sock = FakeSock([hello_frame()])
    run = start_connection(settings, sock, pending=pending)
    try:
        await until(lambda: sock.of("lease.release"))
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
    sock = FakeSock([hello_frame()])
    run = start_connection(settings, sock, outbox=outbox, draining=draining)
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
    run = start_connection(
        settings, FakeSock([hello_frame()]), outbox=outbox, draining=draining, wait=0.2
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
    await until(lambda: waiters)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiters == {}


async def test_lost_connection_fails_waiting_uploads_fast(settings: Settings) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
    metrics = Metrics()
    execution.metrics = metrics
    outbox.metrics = metrics
    session_id = uuid.uuid4()
    sock = FakeSock([hello_frame(sessions={str(session_id): 0})])
    run = start_connection(
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
        await until(lambda: sock.of("artifact.presign"))
        sock.push({"type": "ack", "session_id": str(session_id), "last_seq": 1})
        await until(lambda: outbox.acked_seq(session_id) == 1)
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


class _RunExecution(FakeExecution):
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
        [
            FakeSock([{"ok": False, "error": "database busy"}]),
            FakeSock([hello_frame()]),
            None,
        ]
    )
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connects)
    )
    try:
        await until(lambda: connects.calls >= 2)
        await until(
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
    connects = _Connects([None, None, None, FakeSock([hello_frame()])])
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connects)
    )
    try:
        await until(lambda: connects.calls >= 4)
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
        await until(lambda: calls >= 3)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_observe_loop_samples_guests_at_once_on_a_fresh_host(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from apipi.worker import execution as execution_module
    from apipi.worker.execution import local_execution

    real = execution_module.run_loop
    loop = asyncio.get_running_loop()
    now = [5.0]

    async def instant(_seconds: float) -> None:
        now[0] += 5.0

    async def two_rounds(*args: Any, **kwargs: Any) -> None:
        kwargs.update(sleep=instant, rounds=2)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: now[0])
            await real(*args, **kwargs)

    monkeypatch.setattr(execution_module, "run_loop", two_rounds)
    sampled = settings.model_copy(
        update={"guest_sample_interval": timedelta(seconds=15)}
    )
    execution = local_execution(sampled, outbox=Outbox(), metrics=Metrics())
    samples: list[float] = []

    async def sample() -> None:
        samples.append(now[0])

    monkeypatch.setattr(execution, "_observe_guest_samples", sample)
    await execution.observe_loop()
    assert samples == [5.0]


async def _swallowed_cancel(entered: asyncio.Event) -> None:
    with contextlib.suppress(ValueError):
        await closed_on_cancel(entered)


async def _ends_cancelled(task: "asyncio.Task[Any]") -> None:
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=5)
    assert task in done
    assert task.cancelled()


class _SwallowingSock(FakeSock):
    def __init__(self, block: str, entered: asyncio.Event) -> None:
        super().__init__([hello_frame(), {"type": "noop"}])
        self.block = block
        self.entered = entered
        self.received = 0
        self.blocked = False

    async def send(self, data: str) -> None:
        message = json.loads(data)
        if message.get("type") == self.block and not self.blocked:
            self.blocked = True
            await _swallowed_cancel(self.entered)
        self.sent.append(message)

    async def recv(self) -> Any:
        received = await super().recv()
        self.received += 1
        if self.block == "recv" and self.received == 2:
            self.blocked = True
            await _swallowed_cancel(self.entered)
        return received


@pytest.mark.parametrize("block", ["recv", "heartbeat", "usage", "inventory", "drain"])
async def test_a_cancel_ends_the_connection_when_a_lane_swallows_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, block: str
) -> None:
    monkeypatch.setattr("apipi.worker.client.FIRST_INVENTORY_DELAY", 0.01)
    monkeypatch.setattr("apipi.worker.client.INVENTORY_INTERVAL", 0.05)
    entered = asyncio.Event()
    outbox = Outbox()
    outbox.append(uuid.uuid4(), "usage", {"input": 1})
    execution = FakeExecution(settings, outbox)
    draining = asyncio.Event()
    if block == "drain":
        draining.set()
        killed = 0

        async def kill_unheld(reason: str = "") -> None:
            nonlocal killed
            del reason
            killed += 1
            if killed == 1:
                await _swallowed_cancel(entered)

        monkeypatch.setattr(execution.pool, "kill_unheld", kill_unheld)
    run = start_connection(
        settings,
        _SwallowingSock(block, entered),
        execution=execution,
        outbox=outbox,
        draining=draining,
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _ends_cancelled(run.task)
    finally:
        await run.stop()


async def test_a_lease_release_cancelled_while_it_is_sent_goes_out_after_the_next_hello(
    settings: Settings,
) -> None:
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    entered = asyncio.Event()

    class _BlockingSock(FakeSock):
        async def send(self, data: str) -> None:
            if json.loads(data).get("type") == "lease.release":
                entered.set()
                await asyncio.Event().wait()
            await super().send(data)

    leases = {session_id: lease}
    pending: dict[uuid.UUID, str] = {}
    sock = _BlockingSock([hello_frame(sessions={str(session_id): 0})])
    run = start_connection(settings, sock, leases=leases, pending=pending)
    try:
        await until(lambda: sock.of("heartbeat"))
        release = asyncio.create_task(run.execution.note_stopped(session_id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert leases == {}
        release.cancel()
        await asyncio.gather(release, return_exceptions=True)
        assert pending == {session_id: lease}
    finally:
        await run.stop()
    after = FakeSock([hello_frame()])
    second = start_connection(settings, after, leases=leases, pending=pending)
    try:
        await until(lambda: after.of("lease.release"))
        assert after.of("lease.release")[0]["lease_id"] == lease
        assert pending == {}
    finally:
        await second.stop()


async def test_a_cancel_ends_the_reconnect_loop_when_the_socket_turns_it_into_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _worker_settings(tmp_path)
    execution = _RunExecution(settings, Outbox())
    _patch_run_worker(monkeypatch, execution)
    entered = asyncio.Event()

    class _ClosingSock(FakeSock):
        async def recv(self) -> Any:
            await closed_on_cancel(entered)

    connects = _Connects([_ClosingSock()])
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connects)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _ends_cancelled(task)
        assert connects.calls == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("op", ["turn.cancel", "session.stop"])
async def test_a_command_that_turns_a_cancel_into_an_error_is_not_reported(
    settings: Settings, op: str
) -> None:
    outbox = Outbox()
    execution = FakeExecution(settings, outbox)
    entered = asyncio.Event()

    async def closing(_sid: uuid.UUID) -> None:
        await closed_on_cancel(entered)

    execution.on_cancel = closing
    execution.on_teardown = closing
    session_id = uuid.uuid4()
    lease = str(uuid.uuid4())
    sock = FakeSock([hello_frame(), command_frame(session_id, lease, op)])
    run = start_connection(settings, sock, execution=execution, outbox=outbox)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        (command,) = [
            task
            for task in run.tasks
            if task.get_name() in {"worker_command", "worker_stop"}
        ]
        await _ends_cancelled(command)
        assert outbox.pending(session_id) == []
    finally:
        await run.stop()


async def test_a_cancel_ends_the_delta_flush_when_the_send_turns_it_into_an_error() -> (
    None
):
    entered = asyncio.Event()

    async def send(_wire: dict[str, Any]) -> None:
        await closed_on_cancel(entered)

    relay = DeltaRelay(send, window=0)
    await relay.submit(uuid.uuid4(), uuid.uuid4(), "hello")
    task = relay._flush_task
    assert task is not None
    await asyncio.wait_for(entered.wait(), timeout=5)
    await _ends_cancelled(task)
    assert relay.dropped == 0


async def test_a_cancel_ends_a_worker_loop_whose_round_swallows_it(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from apipi.worker import execution as execution_module
    from apipi.worker.execution import local_execution

    real = execution_module.run_loop

    async def fast(*args: Any, **kwargs: Any) -> None:
        kwargs["interval"] = 0
        await real(*args, **kwargs)

    monkeypatch.setattr(execution_module, "run_loop", fast)
    execution = local_execution(settings, outbox=Outbox(), metrics=Metrics())
    entered = asyncio.Event()

    async def seen(_ids: list[uuid.UUID]) -> None:
        await closed_on_cancel(entered)

    execution.seen_hook = seen
    task = asyncio.create_task(execution.sandbox_seen_loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await _ends_cancelled(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
