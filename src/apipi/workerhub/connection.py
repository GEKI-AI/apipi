import time
import uuid
from dataclasses import dataclass, field

from starlette.websockets import WebSocket


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
