"""The handshake: `register`, `hello`, and the shared-store proof.

Every worker opens `/internal/worker` with a per-worker bearer token,
then sends `register` as its first message. Anything else as the first
message, or a register without `protocol: 2`, is rejected: the API
answers `{"ok": false, "error": ...}` and closes the socket with code
1008. A valid register is answered with `hello`, which carries the
persisted `last_seq` per running session, the lease TTL, the
heartbeat interval, and the `features` the API supports. A `register`
without `features` is a baseline peer; see `peer_features`.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from apipi.protocol.base import ControlMessage
from apipi.protocol.constants import PROTOCOL_VERSION, UNSUPPORTED_PROTOCOL_REASON
from apipi.protocol.control import (
    RevokeEntry,
    TtlEntry,
    WorkerImageInfo,
    keep_valid_images,
)


def keep_features(value: Any) -> Any:
    """Keep the string items of a `features` list; other shapes count as absent."""
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


class RunningSession(ControlMessage):
    session_id: uuid.UUID
    lease_id: uuid.UUID
    last_seq: int = Field(default=0, ge=0)


class RegisterMessage(ControlMessage):
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
    images: list[WorkerImageInfo] | None = None
    version: str | None = None
    features: list[str] | None = None

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("features", mode="before")
    @classmethod
    def _valid_features(cls, value: Any) -> Any:
        return keep_features(value)

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

    @field_validator("images", mode="before")
    @classmethod
    def _valid_images(cls, value: Any) -> Any:
        return keep_valid_images(value)


class StoreCheck(ControlMessage):
    """Shared-filesystem proof the API issues in `hello`.

    Only present with `APIPI_ARTIFACT_STORE=local`. The API writes
    `marker` under the shared store root containing `nonce`; the worker
    must read it back and answer with `store.proof`. Without the same
    filesystem the worker cannot prove it and the register is rejected.
    """

    marker: str
    nonce: str


class StoreProof(ControlMessage):
    """Worker to API. Proof it sees the shared store root."""

    type: Literal["store.proof"] = "store.proof"
    marker: str
    nonce: str


class HelloReply(ControlMessage):
    """API to worker. Answer to a valid `register`.

    `sessions` maps each running session to the seq the API already
    persisted, and the worker replays everything after it. `revoke`
    lists leases the worker must drop, and `ttl` carries the reaper
    idle TTL per session. The API always sets `worker_id`,
    `generation`, and `connection_id`; a worker does not need them to
    run. `connection_id` names this socket in the logs of both sides.
    """

    type: Literal["hello"] = "hello"
    ok: Literal[True] = True
    protocol: Literal[2] = 2
    worker_id: uuid.UUID | None = None
    generation: int | None = None
    connection_id: str | None = None
    lease_ttl_seconds: float = Field(gt=0)
    heartbeat_seconds: float = Field(gt=0)
    sessions: dict[uuid.UUID, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    store_check: StoreCheck | None = None
    revoke: list[RevokeEntry] = Field(default_factory=list)
    ttl: dict[uuid.UUID, TtlEntry] = Field(default_factory=dict)
    features: list[str] | None = None

    @field_validator("features", mode="before")
    @classmethod
    def _valid_features(cls, value: Any) -> Any:
        return keep_features(value)


class RejectMessage(ControlMessage):
    """API to worker. A rejected handshake, sent just before the close."""

    ok: Literal[False] = False
    error: str


class UnsupportedProtocol(ValueError):
    def __init__(self, reason: str = UNSUPPORTED_PROTOCOL_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


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
