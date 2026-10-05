"""The commands the API sent to workers and has not seen acked yet.

Commands are kept per lease, in send order. A command stays until the
worker acks it with `lease.ack`, the lease ends, or it ages out. The
queue only holds the frames and their timing; the hub sends them again
on a reconnect and on a timer. A command is addressed by its lease and
by the worker that holds it, so another replica can later ask the
replica that owns the socket to send it.
"""

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from apipi.protocol import CURSOR_OPS

MAX_PENDING_PER_LEASE = 16


class CommandQueueFull(Exception):
    """A lease already has `MAX_PENDING_PER_LEASE` unacked commands."""


@dataclass
class PendingCommand:
    wire: dict[str, Any]
    lease_id: uuid.UUID
    session_id: uuid.UUID
    worker_id: uuid.UUID | None
    created: float
    sent_at: float | None = None
    sends: int = 0

    @property
    def command_id(self) -> str:
        return str(self.wire.get("id"))

    @property
    def op(self) -> str:
        return str(self.wire.get("op"))


@dataclass
class CommandQueue:
    clock: Callable[[], float] = time.monotonic
    _by_lease: dict[uuid.UUID, list[PendingCommand]] = field(default_factory=dict)
    _turns: dict[uuid.UUID, int] = field(default_factory=dict)

    def push(
        self, wire: dict[str, Any], *, worker_id: uuid.UUID | None = None
    ) -> PendingCommand:
        """Queue one `command` frame behind the earlier ones of its lease."""
        lease_id = uuid.UUID(str(wire["lease_id"]))
        queue = self._by_lease.setdefault(lease_id, [])
        if len(queue) >= MAX_PENDING_PER_LEASE:
            raise CommandQueueFull(str(lease_id))
        entry = PendingCommand(
            wire=wire,
            lease_id=lease_id,
            session_id=uuid.UUID(str(wire["session_id"])),
            worker_id=worker_id,
            created=self.clock(),
        )
        queue.append(entry)
        if entry.op in CURSOR_OPS:
            self._turns[lease_id] = self._turns.get(lease_id, 0) + 1
        return entry

    def turns(self, lease_id: uuid.UUID) -> int:
        """How many `turn.start`, `turn.continue`, and `sandbox.boot` the lease got."""
        return self._turns.get(lease_id, 0)

    def get(self, lease_id: uuid.UUID, command_id: str) -> PendingCommand | None:
        for entry in self._by_lease.get(lease_id, []):
            if entry.command_id == command_id:
                return entry
        return None

    def ack(self, lease_id: uuid.UUID, command_id: str) -> PendingCommand | None:
        """Remove and return one acked command; None when it is not queued."""
        entry = self.get(lease_id, command_id)
        if entry is not None:
            self.discard(entry)
        return entry

    def discard(self, entry: PendingCommand) -> None:
        queue = self._by_lease.get(entry.lease_id)
        if queue is None:
            return
        if entry in queue:
            queue.remove(entry)
        if not queue:
            del self._by_lease[entry.lease_id]

    def forget_turns(self, lease_id: uuid.UUID) -> None:
        if not self._by_lease.get(lease_id):
            self._turns.pop(lease_id, None)

    def drop_lease(self, lease_id: uuid.UUID) -> list[PendingCommand]:
        self._turns.pop(lease_id, None)
        return self._by_lease.pop(lease_id, [])

    def for_lease(self, lease_id: uuid.UUID) -> list[PendingCommand]:
        return list(self._by_lease.get(lease_id, []))

    def has_lease(self, lease_id: uuid.UUID) -> bool:
        return bool(self._by_lease.get(lease_id))

    def leases(self) -> list[uuid.UUID]:
        return list(self._by_lease)

    def for_worker(self, worker_id: uuid.UUID) -> list[PendingCommand]:
        return [entry for entry in self if entry.worker_id == worker_id]

    def mark_sent(self, entry: PendingCommand) -> None:
        entry.sent_at = self.clock()
        entry.sends += 1

    def due(self, interval: float) -> list[PendingCommand]:
        """Commands last sent `interval` seconds ago or more, in send order."""
        now = self.clock()
        return [
            entry
            for entry in self
            if entry.sent_at is not None and now - entry.sent_at >= interval
        ]

    def expired(self, ttl: float) -> list[PendingCommand]:
        """Commands queued `ttl` seconds ago or more."""
        now = self.clock()
        return [entry for entry in self if now - entry.created >= ttl]

    def __iter__(self) -> Iterator[PendingCommand]:
        for queue in list(self._by_lease.values()):
            yield from list(queue)

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._by_lease.values())
