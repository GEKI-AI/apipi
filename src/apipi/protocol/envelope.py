"""Worker to API envelopes: the durable and ephemeral message stream.

After the handshake the worker sends one `WorkerEnvelope` per message:
`{v: 2, session_id, turn_id | null, seq, type, payload}`. `seq` is
monotonic per session and assigned by the worker.

Durable envelopes are ingested idempotently by the API and covered by
the cumulative `ack{last_seq}`. Ephemeral envelopes (streaming deltas)
are at-most-once, never persisted, and never acked.
"""

import uuid
from typing import Any, Literal

from pydantic import Field

from apipi.protocol.base import ControlMessage, EnvelopePayload
from apipi.protocol.constants import (
    DURABLE_MESSAGE_TYPES,
    EPHEMERAL_MESSAGE_TYPES,
)


class UnknownMessageType(ValueError):
    pass


class UsageFailure(EnvelopePayload):
    """A failure the worker serialized into its `usage` envelope."""

    message: str = ""
    code: str = "internal"
    failure_source: str = "internal"
    retryable: bool = False
    upstream_status: int | None = None
    legacy_code: str | None = None
    upstream_attempts: int | None = None


class WorkerEnvelope(ControlMessage):
    """One worker to API envelope: `{v, session_id, turn_id, seq, type, payload}`.

    A frame with `v` is an envelope, a frame without it is a control
    message. `turn_id` is set for turn-scoped types and null otherwise.
    """

    v: Literal[2] = 2
    session_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    seq: int = Field(ge=0)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        session_id: uuid.UUID,
        seq: int,
        type: str,
        payload: EnvelopePayload | dict[str, Any],
        *,
        turn_id: uuid.UUID | None = None,
    ) -> "WorkerEnvelope":
        """Wrap one payload, given as a model or as its wire dict."""
        data = payload.to_wire() if isinstance(payload, EnvelopePayload) else payload
        return cls(
            session_id=session_id, turn_id=turn_id, seq=seq, type=type, payload=data
        )

    def message_class(self) -> Literal["durable", "ephemeral"] | None:
        if self.type in DURABLE_MESSAGE_TYPES:
            return "durable"
        if self.type in EPHEMERAL_MESSAGE_TYPES:
            return "ephemeral"
        return None

    def parsed_payload(self) -> EnvelopePayload:
        model = PAYLOAD_MODELS.get(self.type)
        if model is None:
            raise UnknownMessageType(f"unknown worker message type: {self.type}")
        return model.model_validate(self.payload)


class ItemAddedPayload(EnvelopePayload):
    item_id: uuid.UUID
    item_type: str
    turn_id: uuid.UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ItemDonePayload(EnvelopePayload):
    item_id: uuid.UUID
    turn_id: uuid.UUID | None = None
    data: dict[str, Any] | None = None


class TurnStatusPayload(EnvelopePayload):
    turn_id: uuid.UUID
    status: Literal["started", "completed", "failed", "cancelled"]
    code: str | None = None
    message: str | None = None


class UsagePayload(EnvelopePayload):
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
    failure: UsageFailure | None = None


class ArtifactPresignPayload(EnvelopePayload):
    """Worker asks the API for an artifact upload slot.

    The socket carries only this metadata, never file bytes. The API
    checks quotas before issuing a URL (S3) or reserving the write
    (shared filesystem), and the key is bound under the session prefix.
    """

    request_id: uuid.UUID
    kind: Literal["artifact", "pi_session", "input_image"] = "artifact"
    filename: str | None = None
    content_type: str | None = None
    size: int = Field(ge=1)
    sha256: str | None = None
    turn_id: uuid.UUID | None = None


class ArtifactCompletedPayload(EnvelopePayload):
    """Worker reports an upload or shared-filesystem write is done.

    `upload_id` comes from `artifact.presign.reply`, plus the observed
    size and checksum. Filesystem also sends `path`, the store-root
    relative path the API returned in the reply. No bytes travel here.
    """

    upload_id: uuid.UUID
    path: str | None = None
    name: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    turn_id: uuid.UUID | None = None


class WorkerErrorPayload(EnvelopePayload):
    code: str
    message: str
    turn_id: uuid.UUID | None = None


class WorkerEventPayload(EnvelopePayload):
    """A public session event, applied as stored by the API."""

    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    turn_id: uuid.UUID | None = None


class SessionStatusPayload(EnvelopePayload):
    status: str | None = None
    required_actions: list[Any] | None = None


class SandboxStatusPayload(EnvelopePayload):
    """A sandbox phase transition; the API applies `record_transition`."""

    status: str
    reason: str | None = None
    tenant_id: str | None = None
    worker_id: str | None = None
    image: str | None = None
    image_version: str | None = None
    size: str | None = None
    run_mode: str | None = None
    cause: str | bool | None = None
    cold: bool | None = None
    boot_ms: int | None = None
    lock_wait_ms: int | None = None
    setup_ms: int | None = None
    live_ms: int | None = None


class SessionStoppedPayload(EnvelopePayload):
    """Durable receipt for the wipe after `session.stop`."""

    reason: str = "stop"


class WorkspaceReapedPayload(EnvelopePayload):
    """Durable receipt for an idle-workspace wipe by the reaper."""

    reason: str = "idle"


class LifecycleStartPayload(EnvelopePayload):
    """One live session starting; the API exports it, never the worker."""

    cause: str = "spawn"
    tenant_id: str | None = None
    org_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    key_id: str | None = None
    environment_type: str | None = None
    sandbox_size: str | None = None
    sandbox_image: str | None = None
    image_version: str | None = None
    image_digest: str | None = None
    run_mode: str | None = None
    started_at: str | None = None


class LifecycleStopPayload(EnvelopePayload):
    """One live session ending; the API exports it, never the worker."""

    reason: str = "stop"
    live_ms: int | None = Field(default=None, ge=0)
    start_seq: int | None = Field(default=None, ge=1)
    started_at: str | None = None


class DeltaTextPayload(EnvelopePayload):
    turn_id: uuid.UUID
    text: str


class DeltaReasoningPayload(EnvelopePayload):
    turn_id: uuid.UUID
    text: str


PAYLOAD_MODELS: dict[str, type[EnvelopePayload]] = {
    "item.added": ItemAddedPayload,
    "item.done": ItemDonePayload,
    "turn.status": TurnStatusPayload,
    "usage": UsagePayload,
    "event": WorkerEventPayload,
    "session.status": SessionStatusPayload,
    "artifact.presign": ArtifactPresignPayload,
    "session.stopped": SessionStoppedPayload,
    "workspace.reaped": WorkspaceReapedPayload,
    "lifecycle.start": LifecycleStartPayload,
    "lifecycle.stop": LifecycleStopPayload,
    "artifact.completed": ArtifactCompletedPayload,
    "error": WorkerErrorPayload,
    "sandbox.status": SandboxStatusPayload,
    "delta.text": DeltaTextPayload,
    "delta.reasoning": DeltaReasoningPayload,
}


def parse_envelope(data: dict[str, Any]) -> WorkerEnvelope:
    """Parse and fully validate one worker to API envelope."""
    envelope = WorkerEnvelope.model_validate(data)
    if envelope.message_class() is None:
        raise UnknownMessageType(f"unknown worker message type: {envelope.type}")
    envelope.parsed_payload()
    return envelope
