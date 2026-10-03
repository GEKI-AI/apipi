import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.websockets import WebSocket

from apipi.common.logutil import RateLimitedLog
from apipi.protocol import BASELINE_FEATURES, RunningSession
from apipi.workerhub.writer import ConnectionWriter


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
    connection_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    version: str = ""
    features: frozenset[str] = BASELINE_FEATURES
    disconnect_reason: str | None = None
    warnings: RateLimitedLog = field(
        default_factory=lambda: RateLimitedLog(logging.getLogger("apipi.worker"))
    )
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int = 1000
    close_text: str | None = None
    writer: ConnectionWriter = field(init=False)

    def __post_init__(self) -> None:
        self.writer = ConnectionWriter(self)

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one frame through this connection's writer and wait for it."""
        await self.writer.send(payload)

    def request_close(
        self,
        reason: str,
        *,
        code: int | None = None,
        text: str | None = None,
    ) -> None:
        """Ask the serving task to close this socket. The first reason wins."""
        if self.disconnect_reason is None:
            self.disconnect_reason = reason
            if code is not None:
                self.close_code = code
            self.close_text = text
        self.closing.set()


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
