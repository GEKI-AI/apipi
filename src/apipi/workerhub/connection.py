import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.websockets import WebSocket

from apipi.protocol import RunningSession


@dataclass
class WorkerImage:
    id: str
    version: str
    digest: str
    min_size: str


@dataclass
class WorkerConnection:
    worker_id: uuid.UUID
    generation: int
    websocket: WebSocket
    capacity: int
    memory_mb: int
    run_mode: str
    token_id: uuid.UUID | None = None
    arch: str = ""
    leases: set[uuid.UUID] = field(default_factory=set)
    lease_mem: dict[uuid.UUID, int] = field(default_factory=dict)
    draining: bool = False
    images: dict[str, WorkerImage] = field(default_factory=dict)
    accepts: frozenset[str] = frozenset({"none"})
    store_proof: tuple[str, str] | None = None
    connected_at: float = field(default_factory=time.monotonic)
    last_heartbeat: float | None = None
    last_renewed: float = field(default_factory=time.monotonic)


def claimed_leases(
    running: Sequence[RunningSession | dict[str, Any]] | None,
) -> dict[uuid.UUID, uuid.UUID]:
    """The worker's claim as session_id to lease_id; invalid entries are skipped."""
    claimed: dict[uuid.UUID, uuid.UUID] = {}
    for entry in running or []:
        try:
            parsed = (
                entry
                if isinstance(entry, RunningSession)
                else RunningSession.model_validate(entry)
            )
        except ValidationError:
            continue
        claimed[parsed.session_id] = parsed.lease_id
    return claimed
