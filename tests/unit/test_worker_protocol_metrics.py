import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState
from tests.support.logs import field
from tests.support.prom import metric_line

from apipi.common.metrics import Metrics
from apipi.config import ConfigError, Settings
from apipi.protocol import WorkerCommand, wire_type
from apipi.worker.artifact_upload import upload_via_presign
from apipi.worker.commands import dispatch_command
from apipi.worker.deltas import DeltaRelay
from apipi.worker.outbox import Outbox, OutboxFull
from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.hub import WorkerHub


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


def _ws() -> SimpleNamespace:
    return SimpleNamespace(
        client_state=WebSocketState.CONNECTED, send_json=AsyncMock(), close=AsyncMock()
    )


def _conn(
    worker_id: uuid.UUID | None = None, ws: SimpleNamespace | None = None
) -> WorkerConnection:
    return WorkerConnection(
        worker_id=worker_id or uuid.uuid4(),
        generation=1,
        websocket=cast(Any, ws if ws is not None else _ws()),
        capacity=2,
        memory_mb=4096,
        run_mode="none",
    )


def _wire(op: str = "turn.start") -> dict[str, object]:
    return {
        "type": "command",
        "id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "op": op,
        "payload": {"context": {"model": {"api_key": "sk-secret"}}},
    }


def test_catalogue_series_and_labels() -> None:
    metrics = Metrics()
    metrics.set_worker_connections({"none": 2}, modes={"none"})
    metrics.observe_worker_connect("ok")
    metrics.observe_worker_disconnect("clean")
    metrics.observe_worker_message("in", "heartbeat", 120)
    metrics.observe_worker_handle("heartbeat", 0.002)
    metrics.observe_worker_ingest_batch(seconds=0.01, size=3)
    metrics.observe_worker_command("turn.start", "sent")
    metrics.observe_worker_command_ack("turn.start", 0.2)
    metrics.set_worker_commands_unacked(1)
    metrics.set_worker_send_queue_depth(4)
    metrics.observe_worker_presign("artifact", "ok", 0.1)
    metrics.observe_search("tavily", "ok", 0.3)
    metrics.add_search_inflight(1)
    metrics.observe_event_bus_notify_error()
    metrics.observe_background_loop_error("lease_reaper")
    metrics.set_background_loop_last_run("lease_reaper", 1.0)
    metrics.observe_event_loop_lag(0.01)
    metrics.set_worker_info(worker_id="w1", protocol="2", version="1", run_mode="none")
    metrics.set_worker_connected(True)
    metrics.observe_worker_reconnect("closed")
    metrics.observe_worker_connect_seconds(0.1)
    metrics.set_worker_outbox(messages=1, size=10, oldest_seconds=2.0)
    metrics.observe_worker_outbox_full()
    metrics.observe_worker_ack(0.1)
    metrics.observe_worker_replayed(3)
    metrics.observe_worker_spool_write(0.001)
    metrics.set_worker_spool_bytes(10)
    metrics.observe_worker_command_received("session.stop", "dispatched")
    metrics.observe_worker_command_seconds("session.stop", 0.1)
    metrics.observe_worker_waiter("presign", "ok")
    metrics.observe_worker_delta_dropped("disconnected")
    metrics.set_worker_draining(True)
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_worker_connections", run_mode="none").endswith(
        " 2.0"
    )
    assert metric_line(body, "apipi_worker_connects_total", result="ok")
    assert metric_line(body, "apipi_worker_disconnects_total", reason="clean")
    assert metric_line(
        body, "apipi_worker_messages_total", direction="in", type="heartbeat"
    )
    assert metric_line(
        body,
        "apipi_worker_message_bytes_count",
        direction="in",
        type="heartbeat",
    )
    assert metric_line(body, "apipi_worker_handle_seconds_count", type="heartbeat")
    assert metric_line(
        body, "apipi_worker_commands_total", op="turn.start", result="sent"
    )
    assert metric_line(body, "apipi_worker_presign_total", kind="artifact", result="ok")
    assert metric_line(
        body, "apipi_search_requests_total", provider="tavily", result="ok"
    )
    assert metric_line(body, "apipi_background_loop_errors_total", loop="lease_reaper")
    assert metric_line(
        body,
        "apipi_worker_info",
        worker_id="w1",
        protocol="2",
        version="1",
        run_mode="none",
    )
    assert metric_line(body, "apipi_worker_reconnects_total", reason="closed")
    assert metric_line(
        body,
        "apipi_worker_commands_received_total",
        op="session.stop",
        result="dispatched",
    )
    assert metric_line(body, "apipi_worker_waiter_total", kind="presign", result="ok")
    assert metric_line(body, "apipi_worker_deltas_dropped_total", reason="disconnected")
    for name in (
        "apipi_worker_commands_unacked 1.0",
        "apipi_worker_send_queue_depth 4.0",
        "apipi_search_inflight 1.0",
        "apipi_event_bus_notify_errors_total 1.0",
        "apipi_worker_connected 1.0",
        "apipi_worker_outbox_oldest_seconds 2.0",
        "apipi_worker_outbox_full_total 1.0",
        "apipi_worker_replayed_total 3.0",
        "apipi_worker_spool_bytes 10.0",
        "apipi_worker_draining 1.0",
        "apipi_event_loop_lag_seconds_count 1.0",
    ):
        assert name in body
    labels = {
        part.split("=", 1)[0]
        for line in body.splitlines()
        if line.startswith("apipi_") and "{" in line
        for part in line.split("{", 1)[1].split("}", 1)[0].split(",")
        if part
    }
    assert not labels & {"session_id", "user_id", "lease_id", "request_id"}
    for line in body.splitlines():
        if line.startswith("apipi_") and "worker_id=" in line:
            assert line.startswith("apipi_worker_info{")


def test_wire_type_is_a_fixed_set() -> None:
    assert wire_type({"type": "heartbeat"}) == "heartbeat"
    assert wire_type({"type": "turn.status"}) == "turn.status"
    assert wire_type({"type": "made-up-by-a-peer"}) == "unknown"
    assert wire_type([1]) == "unknown"
    assert wire_type({"type": 5}) == "unknown"


async def test_hub_command_ack_and_retransmit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    ws = _ws()
    conn = _conn(ws=ws)
    wire = _wire()
    lease = uuid.UUID(str(wire["lease_id"]))
    conn.leases.add(lease)
    hub._conns[conn.worker_id] = conn
    hub._set_unacked(lease, wire)
    hub._note_sent(wire)
    body = metrics.scrape().decode()
    assert "apipi_worker_commands_unacked 1.0" in body
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    for _ in range(3):
        await hub.resend_pending(conn)
    retransmits = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker.command.retransmitted"
    ]
    assert len(retransmits) == 1
    from apipi.common.logutil import JsonFormatter

    for record in caplog.records:
        assert "sk-secret" not in JsonFormatter().format(record)
    assert field(retransmits[0], "error_code") == "command_retransmitted"
    assert ws.send_json.await_count == 3
    assert await hub.ack(lease, str(wire["id"])) is True
    body = metrics.scrape().decode()
    assert "apipi_worker_commands_unacked 0.0" in body
    assert metric_line(
        body, "apipi_worker_commands_total", op="turn.start", result="sent"
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_worker_commands_total", op="turn.start", result="retransmitted"
    ).endswith(" 3.0")
    assert metric_line(
        body, "apipi_worker_commands_total", op="turn.start", result="acked"
    ).endswith(" 1.0")
    assert metric_line(body, "apipi_worker_command_ack_seconds_count", op="turn.start")
    assert metric_line(
        body, "apipi_worker_messages_total", direction="out", type="command"
    ).endswith(" 3.0")


async def test_hub_command_ack_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    wire = _wire("session.stop")
    lease = uuid.UUID(str(wire["lease_id"]))
    hub._set_unacked(lease, wire)
    hub._note_sent(wire)
    assert await hub.wait_ack(lease, str(wire["id"]), timeout=0.05) is False
    assert any(
        getattr(record, "event", None) == "worker.command.ack_timeout"
        and getattr(record, "error_code", None) == "command_ack_timeout"
        for record in caplog.records
    )
    from apipi.common.logutil import JsonFormatter

    for record in caplog.records:
        assert "sk-secret" not in JsonFormatter().format(record)
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_commands_total", op="session.stop", result="timeout"
    ).endswith(" 1.0")


async def test_hub_takeover_counts_lease_event() -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    first = _conn()
    first.leases.add(uuid.uuid4())
    await hub.attach(first)
    second = _conn(first.worker_id)
    await hub.attach(second)
    assert first.disconnect_reason == "takeover"
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_lease_events_total", event="taken_over"
    ).endswith(" 1.0")
    assert metric_line(body, "apipi_worker_connections", run_mode="none").endswith(
        " 1.0"
    )


def _append(outbox: Outbox, session: uuid.UUID) -> None:
    outbox.append(
        session, "turn.status", {"turn_id": uuid.uuid4(), "status": "started"}
    )


def test_outbox_metrics_and_warnings(caplog: pytest.LogCaptureFixture) -> None:
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    outbox = Outbox(
        max_messages=10, max_bytes=1024 * 1024, metrics=metrics, session_share=1.0
    )
    session = uuid.uuid4()
    for _ in range(8):
        _append(outbox, session)
    outbox.observe()
    body = metrics.scrape().decode()
    assert "apipi_worker_outbox_messages 8.0" in body
    assert any(
        getattr(record, "event", None) == "worker.outbox.high"
        for record in caplog.records
    )
    outbox.acked(session, 4)
    body = metrics.scrape().decode()
    assert "apipi_worker_ack_seconds_count 4.0" in body
    for _ in range(6):
        _append(outbox, session)
    for _ in range(3):
        with pytest.raises(OutboxFull):
            _append(outbox, session)
    full = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker.outbox.full"
    ]
    assert len(full) == 1
    assert field(full[0], "error_code") == "worker_outbox_full"


def test_outbox_spool_metrics(tmp_path: Path) -> None:
    metrics = Metrics()
    outbox = Outbox(spool_dir=tmp_path, metrics=metrics)
    session = uuid.uuid4()
    _append(outbox, session)
    outbox.observe()
    body = metrics.scrape().decode()
    assert "apipi_worker_spool_write_seconds_count 1.0" in body
    assert "apipi_worker_spool_bytes 0.0" not in body


async def test_dispatch_counts_commands() -> None:
    metrics = Metrics()
    execution = SimpleNamespace(metrics=metrics)
    await dispatch_command(execution, {"op": "turn.cancel", "session_id": "nope"})
    command = WorkerCommand.model_validate(
        {
            "type": "command",
            "id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "lease_id": str(uuid.uuid4()),
            "op": "session.stop",
            "payload": {},
        }
    )
    await dispatch_command(execution, command)
    body = metrics.scrape().decode()
    assert metric_line(
        body,
        "apipi_worker_commands_received_total",
        op="turn.cancel",
        result="rejected",
    ).endswith(" 1.0")
    assert metric_line(
        body,
        "apipi_worker_commands_received_total",
        op="session.stop",
        result="rejected",
    ).endswith(" 1.0")
    assert metric_line(body, "apipi_worker_command_seconds_count", op="session.stop")


async def test_delta_drops_are_counted() -> None:
    metrics = Metrics()
    relay = DeltaRelay(metrics=metrics)
    await relay.submit(uuid.uuid4(), uuid.uuid4(), "hi")
    await relay.flush()
    assert relay.dropped == 1
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_deltas_dropped_total", reason="disconnected"
    ).endswith(" 1.0")


async def test_presign_waiter_timeout_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = Metrics()
    outbox = Outbox(metrics=metrics)
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    with pytest.raises(ConfigError):
        await upload_via_presign(
            outbox,
            {},
            _settings(),
            uuid.uuid4(),
            kind="artifact",
            filename="a.txt",
            content_type=None,
            data=b"abc",
            timeout=0.02,
        )
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_waiter_total", kind="presign", result="timeout"
    ).endswith(" 1.0")
    assert any(
        getattr(record, "event", None) == "worker.waiter.timeout"
        for record in caplog.records
    )


def test_json_roundtrip_hello_carries_connection_id() -> None:
    from apipi.protocol import HelloReply

    reply = HelloReply(lease_ttl_seconds=30, heartbeat_seconds=10, connection_id="abc")
    assert json.loads(json.dumps(reply.to_wire()))["connection_id"] == "abc"
    assert HelloReply(lease_ttl_seconds=30, heartbeat_seconds=10).connection_id is None


class _Sock:
    def __init__(self, fail_after_hello: bool) -> None:
        self._sent_hello = False
        self._fail = fail_after_hello

    async def send(self, _data: str) -> None:
        return None

    async def recv(self) -> str:
        if not self._sent_hello:
            self._sent_hello = True
            return json.dumps(
                {
                    "ok": True,
                    "worker_id": str(uuid.uuid4()),
                    "connection_id": "conn-1",
                    "lease_ttl_seconds": 30,
                    "heartbeat_seconds": 10,
                }
            )
        if self._fail:
            from websockets.exceptions import ConnectionClosedError

            raise ConnectionClosedError(None, None)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Connect:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> "_Connect":
        self.calls += 1
        self._sock = _Sock(fail_after_hello=self.calls == 1)
        return self

    async def __aenter__(self) -> _Sock:
        return self._sock

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _WorkerExecution:
    tracing = None

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics
        self.pool = SimpleNamespace(live=lambda: 0)

    async def _idle(self) -> None:
        await asyncio.Event().wait()

    reap_loop = observe_loop = reap_workspace_loop = sandbox_seen_loop = _idle

    async def close(self) -> None:
        return None


async def test_run_worker_connection_series_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apipi.common.logutil import ContextFilter
    from apipi.worker.client import run_worker

    metrics = Metrics()
    (tmp_path / "worker.token").write_text("secret\n")
    settings = Settings(
        run_mode="none", worker_token_file=str(tmp_path / "worker.token")
    )
    execution = _WorkerExecution(metrics)
    monkeypatch.setattr(
        "apipi.worker.execution.local_execution", lambda *_a, **_k: execution
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability", lambda _s: (metrics, None)
    )
    monkeypatch.setattr("apipi.worker.client._install_drain_signals", lambda _e: None)
    monkeypatch.setattr("apipi.worker.client.random.random", lambda: 1.0)

    async def no_scrape(*_a: object, **_k: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("apipi.worker.scrape.serve_metrics", no_scrape)
    connect = _Connect()
    caplog.set_level(logging.INFO, logger="apipi.worker")
    caplog.handler.addFilter(ContextFilter())
    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connect)
    )
    try:
        for _ in range(300):
            body = metrics.scrape().decode()
            if connect.calls >= 2 and "apipi_worker_connected 1.0" in body:
                break
            await asyncio.sleep(0.02)
        assert connect.calls >= 2
        body = metrics.scrape().decode()
        assert "apipi_worker_connected 1.0" in body
        assert "apipi_worker_connect_seconds_count 2.0" in body
        assert metric_line(
            body, "apipi_worker_reconnects_total", reason="closed"
        ).endswith(" 1.0")
        events = {getattr(record, "event", None): record for record in caplog.records}
        assert field(events["worker.reconnecting"], "attempt") == 1
        assert field(events["worker.reconnecting"], "delay_s") == 0.5
        assert field(events["worker.hello.received"], "connection_id") == "conn-1"
        assert field(events["worker.disconnected"], "unacked_envelopes") == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
