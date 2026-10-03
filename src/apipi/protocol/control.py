"""Control messages that keep a worker connection alive and consistent.

Worker to API: `heartbeat`, `lease.ack`, `lease.release`, `inventory`,
and `sandbox.seen`. API to worker: `lease.revoke` and `inventory.reply`.
None of these are envelopes: they have no `seq` and never enter the outbox.
"""

import uuid
from typing import Any, Literal

from pydantic import Field, field_validator

from apipi.protocol.base import ControlMessage


class WorkerImageInfo(ControlMessage):
    """One sandbox image a worker can start."""

    id: str
    version: str = ""
    digest: str = ""
    min_size: str = "S"

    @field_validator("version", "digest", "min_size", mode="before")
    @classmethod
    def _text_or_default(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str):
            return value
        return cls.model_fields[info.field_name].default


def keep_valid_images(value: Any) -> Any:
    """Drop image entries that are not objects with a non-empty string `id`."""
    if not isinstance(value, list):
        return value
    kept = []
    for item in value:
        if isinstance(item, WorkerImageInfo):
            kept.append(item)
        elif isinstance(item, dict):
            image_id = item.get("id")
            if isinstance(image_id, str) and image_id:
                kept.append(item)
    return kept


class LeaseAck(ControlMessage):
    """Worker to API. Command receipt; retransmits of the same id are safe."""

    type: Literal["lease.ack"] = "lease.ack"
    id: uuid.UUID
    lease_id: uuid.UUID


class LeaseRelease(ControlMessage):
    """Worker to API. The worker dropped the session."""

    type: Literal["lease.release"] = "lease.release"
    session_id: uuid.UUID
    lease_id: uuid.UUID


class LeaseRevoke(ControlMessage):
    """API to worker. The lease is no longer valid."""

    type: Literal["lease.revoke"] = "lease.revoke"
    session_id: uuid.UUID
    lease_id: uuid.UUID


class RevokeEntry(ControlMessage):
    """One revocation inside `hello` or `inventory.reply`.

    A missing `lease_id` means the worker holds no lease for the
    session: the API found no session row, so the worker wipes the
    leftover workspace directory.
    """

    type: Literal["lease.revoke"] = "lease.revoke"
    session_id: uuid.UUID
    lease_id: uuid.UUID | None = None


class TtlEntry(ControlMessage):
    """The effective reaper TTL of one session.

    `idle_since_epoch` is the idle baseline (the session row's last
    touch), so the worker reaper clock does not restart on every reply.
    """

    idle_ttl_seconds: float | None = None
    env_type: str | None = None
    idle_since_epoch: float | None = None


class InventoryEntry(ControlMessage):
    """One session the worker reports.

    A missing `lease_id` reports an on-disk workspace the worker holds
    no lease for: the API answers with a reaper TTL while the session
    is leased and a revoke (which wipes the dir) once it is free.
    """

    session_id: uuid.UUID
    lease_id: uuid.UUID | None = None
    last_seq: int = Field(default=0, ge=0)


class InventoryMessage(ControlMessage):
    """Worker to API. Periodic live-set report; also drives reconcile."""

    type: Literal["inventory"] = "inventory"
    sessions: list[InventoryEntry] = Field(default_factory=list)


class InventoryReply(ControlMessage):
    """API to worker. Revocations plus TTLs for the reaper."""

    type: Literal["inventory.reply"] = "inventory.reply"
    revoke: list[RevokeEntry] = Field(default_factory=list)
    ttl: dict[uuid.UUID, TtlEntry] = Field(default_factory=dict)


class SandboxSeenMessage(ControlMessage):
    """Worker to API. Periodic live sandbox ids; idempotent like touch_seen."""

    type: Literal["sandbox.seen"] = "sandbox.seen"
    session_ids: list[uuid.UUID] = Field(default_factory=list)


class HeartbeatMessage(ControlMessage):
    """Worker to API. Renews the leases and refreshes the worker's capacity.

    Every field except `type` is optional. `accepts` lists the run
    modes the worker takes, `image_store_version` identifies its image
    store, and `drain` marks a worker that takes no new sessions.
    """

    type: Literal["heartbeat"] = "heartbeat"
    capacity: int | None = Field(default=None, ge=1)
    memory_mb: int | None = Field(default=None, ge=1)
    run_mode: str | None = None
    accepts: list[str] | None = None
    arch: str | None = None
    image_store_version: str | None = None
    drain: bool | None = None
    images: list[WorkerImageInfo] | None = None

    @field_validator("images", mode="before")
    @classmethod
    def _valid_images(cls, value: Any) -> Any:
        return keep_valid_images(value)
