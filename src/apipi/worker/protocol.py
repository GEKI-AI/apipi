"""Worker protocol v2 message schemas.

This module is the single definition of the worker protocol wire
shapes. Both the API (`src/apipi/api/workers.py`,
`src/apipi/worker/hub.py`) and the worker (`run_worker`) build and
parse messages through these models, so a shape can only change here.

Protocol v2 (see `specs/decisions/0015-worker-protocol-v2.md`):

* Every worker opens `/internal/worker` with a per-worker bearer
  token, then sends `register{protocol: 2, ...}` as its first
  message. Anything else as the first message, or a register without
  `protocol: 2`, is rejected: the API answers
  `{"ok": false, "error": ...}` and closes the socket with code 1008.
* The API answers a valid register with `hello.reply`, which carries
  the persisted `last_seq` per running session. The worker replays
  everything after that seq.
* After the handshake the worker sends one `WorkerEnvelope` per
  message: `{v: 2, session_id, turn_id | null, seq, type, payload}`.
  `seq` is monotonic per session and assigned by the worker.
* Message classes: **durable** messages are ingested idempotently by
  the API and covered by the cumulative `ack{last_seq}`; **ephemeral**
  messages (streaming deltas) are at-most-once, never persisted, and
  never acked.

Only the handshake and the version check were wired first. Durable
ingest, the outbox, replay, and the cumulative ack are wired now:
the API ingests durable envelopes in batches (about 50ms or 100
messages), one transaction per batch, and sends the cumulative ack
after commit. The worker keeps unacked envelopes in a bounded outbox
(`src/apipi/worker/outbox.py`), with an optional disk spool, and
replays everything after `hello.reply` on reconnect.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROTOCOL_VERSION = 2

WORKER_CLOSE_CODE = 1008
UNSUPPORTED_PROTOCOL_REASON = "unsupported_protocol"

DURABLE_MESSAGE_TYPES = frozenset(
    {
        "item.added",
        "item.done",
        "turn.status",
        "usage",
        "event",
        "session.status",
        "artifact.completed",
        "error",
        "sandbox.status",
    }
)
EPHEMERAL_MESSAGE_TYPES = frozenset({"delta.text", "delta.reasoning"})
WORKER_MESSAGE_TYPES = DURABLE_MESSAGE_TYPES | EPHEMERAL_MESSAGE_TYPES

COMMAND_OPS = frozenset(
    {"turn.start", "turn.cancel", "turn.continue", "session.stop", "sandbox.boot"}
)

OUTBOX_BOUND = 10_000
MAX_MESSAGE_BYTES = 1_000_000


class WireModel(BaseModel):
    """Wire messages ignore unknown fields so a newer peer still parses."""

    model_config = ConfigDict(extra="ignore")


class StrictPayload(BaseModel):
    """Event payloads forbid unknown fields so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class RunningSession(WireModel):
    session_id: uuid.UUID
    lease_id: uuid.UUID
    last_seq: int = Field(default=0, ge=0)


class RegisterMessage(WireModel):
    type: Literal["register"] = "register"
    protocol: int
    id: uuid.UUID | None = Field(default=None, alias="id")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    accepts: list[str] | None = None
    running: list[RunningSession] = Field(default_factory=list)
    capacity: int = Field(default=1, ge=1)
    memory_mb: int | None = Field(default=None, ge=1)
    run_mode: str
    arch: str = ""
    images: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @field_validator("run_mode")
    @classmethod
    def _strip_run_mode(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("run_mode is required")
        return stripped

    @field_validator("accepts")
    @classmethod
    def _check_accepts(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            text = item.strip().lower() if isinstance(item, str) else ""
            if text not in {"none", "microvm"}:
                raise ValueError("accepts must be a list from none,microvm")
            if text not in cleaned:
                cleaned.append(text)
        if not cleaned:
            raise ValueError("accepts must be a list from none,microvm")
        return cleaned


class HelloReply(WireModel):
    type: Literal["hello"] = "hello"
    ok: Literal[True] = True
    protocol: Literal[2] = 2
    worker_id: uuid.UUID
    generation: int
    sessions: dict[uuid.UUID, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class WorkerEnvelope(WireModel):
    v: Literal[2] = 2
    session_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    seq: int = Field(ge=0)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def message_class(self) -> Literal["durable", "ephemeral"] | None:
        if self.type in DURABLE_MESSAGE_TYPES:
            return "durable"
        if self.type in EPHEMERAL_MESSAGE_TYPES:
            return "ephemeral"
        return None

    def parsed_payload(self) -> StrictPayload:
        model = PAYLOAD_MODELS.get(self.type)
        if model is None:
            raise UnknownMessageType(f"unknown worker message type: {self.type}")
        return model.model_validate(self.payload)


class ItemAddedPayload(StrictPayload):
    item_id: uuid.UUID
    item_type: str
    turn_id: uuid.UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ItemDonePayload(StrictPayload):
    item_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    data: dict[str, Any] | None = None


class TurnStatusPayload(StrictPayload):
    turn_id: uuid.UUID
    status: Literal["started", "completed", "failed", "cancelled"]
    code: str | None = None
    message: str | None = None


class UsagePayload(StrictPayload):
    turn_id: uuid.UUID
    model: str | None = None
    status: str | None = None
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    request_id: str | None = None
    user_id: str | None = None
    error_code: str | None = None
    artifact_bytes: int | None = Field(default=None, ge=0)
    tool_names: list[str] | None = None
    tool_counts: dict[str, int] | None = None
    mcp_names: list[str] | None = None
    mcp_counts: dict[str, int] | None = None
    failure: dict[str, Any] | None = None


class ArtifactCompletedPayload(StrictPayload):
    artifact_id: uuid.UUID
    name: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class WorkerErrorPayload(StrictPayload):
    code: str
    message: str
    turn_id: uuid.UUID | None = None


class WorkerEventPayload(StrictPayload):
    """A public session event, applied as stored by the API."""

    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    turn_id: uuid.UUID | None = None


class SessionStatusPayload(StrictPayload):
    status: str | None = None
    required_actions: list[Any] | None = None


class SandboxStatusPayload(StrictPayload):
    status: str
    reason: str | None = None


class DeltaTextPayload(StrictPayload):
    turn_id: uuid.UUID
    text: str


class DeltaReasoningPayload(StrictPayload):
    turn_id: uuid.UUID
    text: str


PAYLOAD_MODELS: dict[str, type[StrictPayload]] = {
    "item.added": ItemAddedPayload,
    "item.done": ItemDonePayload,
    "turn.status": TurnStatusPayload,
    "usage": UsagePayload,
    "event": WorkerEventPayload,
    "session.status": SessionStatusPayload,
    "artifact.completed": ArtifactCompletedPayload,
    "error": WorkerErrorPayload,
    "sandbox.status": SandboxStatusPayload,
    "delta.text": DeltaTextPayload,
    "delta.reasoning": DeltaReasoningPayload,
}


class WorkerCommand(WireModel):
    """API to worker. `command_id` travels on the wire as `id`."""

    type: Literal["command"] = "command"
    command_id: uuid.UUID = Field(validation_alias="id", serialization_alias="id")
    session_id: uuid.UUID
    lease_id: uuid.UUID
    op: str
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CumulativeAck(WireModel):
    """API to worker. Cumulative ack for durable envelopes (wired later)."""

    type: Literal["ack"] = "ack"
    session_id: uuid.UUID
    last_seq: int = Field(ge=0)


class LeaseAck(WireModel):
    """Worker to API. Command receipt; retransmits of the same id are safe."""

    type: Literal["lease.ack"] = "lease.ack"
    id: uuid.UUID
    lease_id: uuid.UUID


class LeaseRelease(WireModel):
    """Worker to API. The worker dropped the session."""

    type: Literal["lease.release"] = "lease.release"
    session_id: uuid.UUID
    lease_id: uuid.UUID


class LeaseRevoke(WireModel):
    """API to worker. The lease is no longer valid."""

    type: Literal["lease.revoke"] = "lease.revoke"
    session_id: uuid.UUID
    lease_id: uuid.UUID


class HeartbeatMessage(WireModel):
    type: Literal["heartbeat"] = "heartbeat"
    capacity: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=1)
    run_mode: str | None = None
    arch: str | None = None
    drain: bool | None = None
    images: list[dict[str, Any]] | None = None


class WorkerEventMessage(WireModel):
    """Legacy worker to API event (replaced by WorkerEnvelope over time)."""

    type: Literal["event"] = "event"
    lease_id: uuid.UUID
    event_type: str
    data: dict[str, Any] | None = None


class UnsupportedProtocol(ValueError):
    def __init__(self, reason: str = UNSUPPORTED_PROTOCOL_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownMessageType(ValueError):
    pass


def parse_register(data: dict[str, Any]) -> RegisterMessage:
    """Parse the first worker message.

    Raises UnsupportedProtocol when the peer does not speak v2 (missing
    or wrong `protocol`), so the caller can close with code 1008 and a
    clear reason. Raises ValidationError for other bad registers.
    """
    protocol = data.get("protocol")
    if protocol != PROTOCOL_VERSION:
        raise UnsupportedProtocol()
    try:
        return RegisterMessage.model_validate(data)
    except ValidationError:
        raise


def parse_envelope(data: dict[str, Any]) -> WorkerEnvelope:
    """Parse and fully validate one worker to API envelope."""
    envelope = WorkerEnvelope.model_validate(data)
    if envelope.message_class() is None:
        raise UnknownMessageType(f"unknown worker message type: {envelope.type}")
    envelope.parsed_payload()
    return envelope
