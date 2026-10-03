"""Parse one incoming socket message into its model, by `type`."""

from typing import Any

from pydantic import BaseModel

from apipi.protocol.base import ControlMessage
from apipi.protocol.commands import WorkerCommand
from apipi.protocol.control import (
    HeartbeatMessage,
    InventoryMessage,
    InventoryReply,
    LeaseAck,
    LeaseRelease,
    LeaseRevoke,
    SandboxSeenMessage,
)
from apipi.protocol.handshake import (
    HelloReply,
    RegisterMessage,
    RejectMessage,
    StoreProof,
)
from apipi.protocol.replies import (
    ArtifactPresignReply,
    CumulativeAck,
    SearchReply,
    SearchRequest,
)

WORKER_MESSAGE_MODELS: dict[str, type[ControlMessage]] = {
    "register": RegisterMessage,
    "heartbeat": HeartbeatMessage,
    "lease.ack": LeaseAck,
    "lease.release": LeaseRelease,
    "store.proof": StoreProof,
    "inventory": InventoryMessage,
    "sandbox.seen": SandboxSeenMessage,
    "search.request": SearchRequest,
}

API_MESSAGE_MODELS: dict[str, type[ControlMessage]] = {
    "hello": HelloReply,
    "error": RejectMessage,
    "command": WorkerCommand,
    "ack": CumulativeAck,
    "artifact.presign.reply": ArtifactPresignReply,
    "search.reply": SearchReply,
    "inventory.reply": InventoryReply,
    "lease.revoke": LeaseRevoke,
}


def parse_worker_message(data: dict[str, Any]) -> BaseModel | None:
    """Parse a worker to API control message; None for an unknown `type`.

    Raises ValidationError when the type is known and the fields are not
    valid. Envelopes are not control messages: use `parse_envelope`.
    """
    model = WORKER_MESSAGE_MODELS.get(str(data.get("type")))
    if model is None:
        return None
    return model.model_validate(data)


def parse_api_message(data: dict[str, Any]) -> BaseModel | None:
    """Parse an API to worker message; None for an unknown `type`.

    Raises ValidationError when the type is known and the fields are not
    valid.
    """
    model = API_MESSAGE_MODELS.get(str(data.get("type")))
    if model is None:
        return None
    return model.model_validate(data)
