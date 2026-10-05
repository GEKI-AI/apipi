"""Worker side of the delivery rules of #488: features, acks, unknown input."""

import asyncio
import uuid
from typing import Any

import pytest
from tests.support.prom import metric_line
from tests.unit.test_worker_robustness import (
    _command,
    _Execution,
    _hello,
    _Sock,
    _start,
    _wait_for,
)

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.protocol import SUPPORTED_FEATURES, strict_parse
from apipi.worker.artifact_upload import (
    PresignFuture,
    fail_lost_presign_waiters,
    handle_presign_reply,
)
from apipi.worker.outbox import FeatureUnsupported, Outbox


@pytest.mark.parametrize("features", [sorted(SUPPORTED_FEATURES), None])
async def test_stop_is_acked_on_receipt_or_after_session_stopped_is_acked(
    settings: Settings, features: list[str] | None
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    release = asyncio.Event()

    async def slow_teardown(session_id: uuid.UUID) -> None:
        await release.wait()
        outbox.append(session_id, "lifecycle.stop", {"reason": "stop"})
        await execution.note_stopped(session_id)

    execution.on_teardown = slow_teardown
    session_id = uuid.uuid4()
    command = _command(session_id, str(uuid.uuid4()), "session.stop")
    hello = _hello() if features is None else _hello(features=features)
    sock = _Sock([hello, command])
    run = _start(settings, sock, execution=execution, outbox=outbox)
    try:
        await _wait_for(lambda: execution.teardowns)
        if features is None:
            await asyncio.sleep(0.1)
            assert sock.of("lease.ack") == []
        else:
            await _wait_for(lambda: sock.of("lease.ack"))
        release.set()
        await _wait_for(lambda: sock.of("session.stopped"))
        stopped = sock.of("session.stopped")[0]
        assert [m["type"] for m in sock.sent if "seq" in m] == [
            "lifecycle.stop",
            "session.stopped",
        ]
        if features is None:
            await asyncio.sleep(0.1)
            assert sock.of("lease.ack") == []
        sock.push(
            {
                "type": "ack",
                "session_id": str(session_id),
                "last_seq": stopped["seq"],
            }
        )
        await _wait_for(lambda: session_id not in run.leases and sock.of("lease.ack"))
        assert [ack["id"] for ack in sock.of("lease.ack")] == [command["id"]]
        assert sock.of("lease.release") == []
        kinds = [m.get("type") for m in sock.sent]
        acked_after_stopped = kinds.index("lease.ack") > kinds.index("session.stopped")
        assert acked_after_stopped is (features is None)
    finally:
        await run.stop()


async def test_unknown_op_type_and_fields_are_counted_not_acked(
    settings: Settings,
) -> None:
    metrics = Metrics()
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    execution.metrics = metrics
    session_id = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    unknown_op = _command(session_id, lease_id, "turn.teleport")
    newer = _command(session_id, lease_id, "turn.start", shiny="new")
    sock = _Sock([_hello(), unknown_op, {"type": "future.message"}, newer])
    with strict_parse(False):
        run = _start(settings, sock, execution=execution, outbox=outbox)
        try:
            await _wait_for(lambda: execution.turns)
            await _wait_for(lambda: sock.of("lease.ack"))
        finally:
            await run.stop()
    assert [ack["id"] for ack in sock.of("lease.ack")] == [newer["id"]]
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_worker_protocol_total", event="unknown_op")
    assert metric_line(body, "apipi_worker_protocol_total", event="unknown_type")
    assert metric_line(
        body, "apipi_worker_commands_received_total", op="unknown", result="unknown_op"
    )


async def test_a_new_context_field_does_not_fail_the_turn(settings: Settings) -> None:
    from apipi.protocol import TurnStartCommandPayload
    from apipi.worker.commands import _command_turn_context

    metrics = Metrics()
    context = {
        "session": {"environment": {"type": "none"}, "key_id": "k", "tomorrow": 1},
        "tomorrows_section": {"a": 1},
    }
    payload = TurnStartCommandPayload.model_validate(
        {"tenant_id": str(uuid.uuid4()), "context": context}
    )
    with strict_parse(False):
        parsed = _command_turn_context("turn.start", payload, metrics)
    assert parsed is not None and parsed["session"]["key_id"] == "k"
    body = metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_protocol_total", event="unknown_field"
    ).endswith(" 2.0")


async def test_search_is_not_wired_when_the_api_does_not_advertise_it(
    settings: Settings,
) -> None:
    outbox = Outbox()
    execution = _Execution(settings, outbox)
    execution.__dict__["search_sender"] = None
    sock = _Sock([_hello(features=["presign"])])
    run = _start(settings, sock, execution=execution, outbox=outbox)
    try:
        await _wait_for(lambda: execution.socket_open)
        assert execution.__dict__["search_sender"] is None
        assert outbox.peer_features == frozenset({"presign"})
    finally:
        await run.stop()


def test_outbox_refuses_a_type_the_api_did_not_advertise() -> None:
    outbox = Outbox()
    outbox.peer_features = frozenset({"search"})
    session_id = uuid.uuid4()
    with pytest.raises(FeatureUnsupported):
        outbox.append(session_id, "artifact.presign", {"request_id": str(uuid.uuid4())})
    outbox.peer_features = frozenset({"presign"})
    envelope = outbox.append(
        session_id,
        "artifact.presign",
        {"request_id": str(uuid.uuid4()), "size": 1},
    )
    assert envelope["seq"] == 1


async def test_a_presign_waiter_survives_a_reconnect_until_its_envelope_is_acked() -> (
    None
):
    outbox = Outbox()
    session_id = uuid.uuid4()
    request_id = uuid.uuid4()
    envelope = outbox.append(
        session_id,
        "artifact.presign",
        {"request_id": str(request_id), "size": 1},
    )
    future = PresignFuture(session_id, loop=asyncio.get_running_loop())
    future.seq = int(envelope["seq"])
    waiters: dict[uuid.UUID, Any] = {request_id: future}
    assert fail_lost_presign_waiters(waiters, outbox) == 0
    assert not future.done()
    reply = {
        "type": "artifact.presign.reply",
        "session_id": str(session_id),
        "request_id": str(request_id),
        "ok": True,
        "upload_id": str(uuid.uuid4()),
    }
    assert handle_presign_reply(waiters, reply) is True
    assert future.result()["upload_id"] == reply["upload_id"]


def test_command_queue_keeps_order_caps_the_size_and_ages_out() -> None:
    from apipi.workerhub.command_queue import (
        MAX_PENDING_PER_LEASE,
        CommandQueue,
        CommandQueueFull,
    )

    now = [100.0]
    queue = CommandQueue(clock=lambda: now[0])
    lease_id, session_id = uuid.uuid4(), uuid.uuid4()

    def wire() -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "op": "turn.cancel",
            "lease_id": str(lease_id),
            "session_id": str(session_id),
        }

    entries = [queue.push(wire()) for _ in range(MAX_PENDING_PER_LEASE)]
    with pytest.raises(CommandQueueFull):
        queue.push(wire())
    assert queue.for_lease(lease_id) == entries
    assert queue.ack(lease_id, entries[3].command_id) is entries[3]
    assert queue.ack(lease_id, entries[3].command_id) is None
    assert len(queue) == MAX_PENDING_PER_LEASE - 1
    queue.mark_sent(entries[0])
    now[0] += 6
    assert queue.due(5.0) == [entries[0]]
    assert queue.expired(30.0) == []
    now[0] += 30
    assert len(queue.expired(30.0)) == MAX_PENDING_PER_LEASE - 1
    assert len(queue.drop_lease(lease_id)) == MAX_PENDING_PER_LEASE - 1
    assert not queue.has_lease(lease_id)
