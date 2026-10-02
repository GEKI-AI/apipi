import uuid

import pytest
from pydantic import ValidationError

from apipi.worker.hub import COMMAND_OPS, WORKER_IN, worker_ws_url
from apipi.worker.protocol import (
    PAYLOAD_MODELS,
    PROTOCOL_VERSION,
    UNSUPPORTED_PROTOCOL_REASON,
    WORKER_CLOSE_CODE,
    WORKER_MESSAGE_TYPES,
    CumulativeAck,
    HelloReply,
    LeaseAck,
    LeaseRelease,
    LeaseRevoke,
    UnknownMessageType,
    UnsupportedProtocol,
    WorkerCommand,
    WorkerEnvelope,
    WorkerEventMessage,
    parse_envelope,
    parse_register,
)


def test_worker_protocol_commands() -> None:
    assert "hello" not in WORKER_IN
    assert {
        "turn.start",
        "turn.cancel",
        "turn.continue",
        "session.stop",
        "sandbox.boot",
    } == COMMAND_OPS
    assert "register" in WORKER_IN
    assert "heartbeat" in WORKER_IN
    assert "lease.ack" in WORKER_IN
    assert "event" in WORKER_IN


def test_worker_ws_url() -> None:
    assert (
        worker_ws_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/internal/worker"
    )
    assert worker_ws_url("https://api.example") == "wss://api.example/internal/worker"


def _register(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": "register",
        "protocol": PROTOCOL_VERSION,
        "capacity": 2,
        "run_mode": "none",
    }
    base.update(overrides)
    return base


def test_parse_register_accepts_v2_with_accepts_room() -> None:
    register = parse_register(
        _register(accepts=["none", "microvm"], running=[], capabilities={"note": "x"})
    )
    assert register.protocol == PROTOCOL_VERSION
    assert register.accepts == ["none", "microvm"]
    assert register.running == []
    assert register.capacity == 2


def test_parse_register_rejects_bad_accepts() -> None:
    with pytest.raises(ValidationError):
        parse_register(_register(accepts=["chat"]))
    with pytest.raises(ValidationError):
        parse_register(_register(accepts=[]))


def test_parse_register_rejects_v1() -> None:
    with pytest.raises(UnsupportedProtocol) as exc:
        parse_register({"type": "register", "capacity": 1, "run_mode": "none"})
    assert exc.value.reason == UNSUPPORTED_PROTOCOL_REASON
    assert WORKER_CLOSE_CODE == 1008


def test_parse_register_rejects_wrong_protocol() -> None:
    with pytest.raises(UnsupportedProtocol):
        parse_register(_register(protocol=1))
    with pytest.raises(UnsupportedProtocol):
        parse_register(_register(protocol=3))


def test_parse_register_rejects_bad_shape() -> None:
    with pytest.raises(ValidationError):
        parse_register(_register(run_mode=""))
    with pytest.raises(ValidationError):
        parse_register(_register(capacity=0))


def test_envelope_validation_and_classes() -> None:
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    durable = WorkerEnvelope.model_validate(
        {
            "v": 2,
            "session_id": str(session_id),
            "turn_id": str(turn_id),
            "seq": 4,
            "type": "delta.text",
            "payload": {"turn_id": str(turn_id), "text": "hi"},
        }
    )
    assert durable.message_class() == "ephemeral"
    assert durable.seq == 4
    with pytest.raises(ValidationError):
        WorkerEnvelope.model_validate(
            {
                "v": 1,
                "session_id": str(session_id),
                "seq": 0,
                "type": "usage",
                "payload": {},
            }
        )


def test_envelope_rejects_unknown_type() -> None:
    with pytest.raises(UnknownMessageType):
        parse_envelope(
            {
                "v": 2,
                "session_id": str(uuid.uuid4()),
                "seq": 0,
                "type": "nope.unknown",
                "payload": {},
            }
        )


def test_envelope_rejects_bad_payload() -> None:
    with pytest.raises(ValidationError):
        parse_envelope(
            {
                "v": 2,
                "session_id": str(uuid.uuid4()),
                "seq": 0,
                "type": "usage",
                "payload": {"prompt_tokens": -1, "turn_id": str(uuid.uuid4())},
            }
        )


def test_every_message_type_round_trips() -> None:
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    item_id = uuid.uuid4()
    payloads: dict[str, dict[str, object]] = {
        "item.added": {
            "item_id": str(item_id),
            "item_type": "message",
            "data": {"text": "hi"},
        },
        "item.done": {"item_id": str(item_id), "data": {"note": "done"}},
        "turn.status": {"turn_id": str(turn_id), "status": "completed"},
        "usage": {
            "turn_id": str(turn_id),
            "model": "test",
            "prompt_tokens": 3,
            "completion_tokens": 4,
        },
        "event": {
            "type": "agent.session.turn.retrying",
            "data": {"turn_id": str(turn_id)},
            "turn_id": str(turn_id),
        },
        "session.status": {
            "status": "idle",
            "required_actions": [],
        },
        "session.stopped": {"reason": "stop"},
        "workspace.reaped": {"reason": "idle"},
        "lifecycle.start": {"cause": "spawn"},
        "lifecycle.stop": {"reason": "stop", "live_ms": 3},
        "artifact.presign": {
            "request_id": str(uuid.uuid4()),
            "kind": "artifact",
            "filename": "out.txt",
            "content_type": "text/plain",
            "size": 12,
        },
        "artifact.completed": {
            "upload_id": str(uuid.uuid4()),
            "name": "out.txt",
            "size": 12,
        },
        "error": {"code": "worker_test", "message": "boom"},
        "sandbox.status": {"status": "ready"},
        "delta.text": {"turn_id": str(turn_id), "text": "hi"},
        "delta.reasoning": {"turn_id": str(turn_id), "text": "hmm"},
    }
    assert set(payloads) == WORKER_MESSAGE_TYPES
    assert set(payloads) == set(PAYLOAD_MODELS)
    for seq, (msg_type, payload) in enumerate(sorted(payloads.items())):
        envelope = parse_envelope(
            {
                "v": 2,
                "session_id": str(session_id),
                "turn_id": str(turn_id),
                "seq": seq,
                "type": msg_type,
                "payload": payload,
            }
        )
        assert envelope.message_class() in {"durable", "ephemeral"}
        assert (
            envelope.parsed_payload().model_dump(mode="json", exclude_none=True)
            == payload
        )
        round_tripped = WorkerEnvelope.model_validate(envelope.model_dump(mode="json"))
        assert round_tripped == envelope


def test_api_to_worker_messages_round_trip() -> None:
    session_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    command_id = uuid.uuid4()
    command = WorkerCommand.model_validate(
        {
            "type": "command",
            "id": str(command_id),
            "session_id": str(session_id),
            "lease_id": str(lease_id),
            "op": "turn.start",
            "payload": {"text": "hi"},
        }
    )
    assert command.command_id == command_id
    assert command.op in COMMAND_OPS
    assert WorkerCommand.model_validate(command.model_dump(by_alias=True)) == command
    ack = CumulativeAck.model_validate(
        {"type": "ack", "session_id": str(session_id), "last_seq": 7}
    )
    assert CumulativeAck.model_validate(ack.model_dump(mode="json")) == ack
    for model, raw in (
        (
            LeaseAck,
            {"type": "lease.ack", "id": str(command_id), "lease_id": str(lease_id)},
        ),
        (
            LeaseRelease,
            {
                "type": "lease.release",
                "session_id": str(session_id),
                "lease_id": str(lease_id),
            },
        ),
        (
            LeaseRevoke,
            {
                "type": "lease.revoke",
                "session_id": str(session_id),
                "lease_id": str(lease_id),
            },
        ),
    ):
        parsed = model.model_validate(raw)
        assert model.model_validate(parsed.model_dump(mode="json")) == parsed


def test_register_and_hello_reply_round_trip() -> None:
    worker_id = uuid.uuid4()
    register = parse_register(
        _register(
            id=str(worker_id),
            running=[
                {
                    "session_id": str(uuid.uuid4()),
                    "lease_id": str(uuid.uuid4()),
                    "last_seq": 9,
                }
            ],
        )
    )
    assert register.id == worker_id
    assert register.running[0].last_seq == 9
    reply = HelloReply.model_validate(
        {
            "type": "hello",
            "ok": True,
            "protocol": 2,
            "worker_id": str(worker_id),
            "generation": 3,
            "sessions": {str(uuid.uuid4()): 9},
        }
    )
    assert reply.protocol == 2
    assert HelloReply.model_validate(reply.model_dump(mode="json")) == reply
    event = WorkerEventMessage.model_validate(
        {
            "type": "event",
            "lease_id": str(uuid.uuid4()),
            "event_type": "agent.session.error",
            "data": {"message": "x"},
        }
    )
    assert event.event_type == "agent.session.error"
