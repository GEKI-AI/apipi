"""A shared in-process stand-in for Postgres LISTEN/NOTIFY between replicas.

Every replica gets its own bus. Messages addressed to an instance go
through JSON and arrive on a later loop turn, like a notification, and
a payload over the 8000 byte `NOTIFY` limit raises so a test fails when
a body that is too big is forwarded. A wake for a stored event reaches
the other replicas' subscribers, like the real bus.
"""

import asyncio
import json
import uuid
from typing import Any

from apipi.common.event_bus import InMemoryEventBus, message_seq, wake_message

NOTIFY_LIMIT = 8000


class NotifyNetwork:
    def __init__(self) -> None:
        self.buses: list[NotifyBus] = []
        self.sent: list[tuple[str, dict[str, Any], int]] = []
        self.dropped: set[str] = set()

    def bus(self) -> "NotifyBus":
        created = NotifyBus(self)
        self.buses.append(created)
        return created


class NotifyBus(InMemoryEventBus):
    forwards = True

    def __init__(self, network: NotifyNetwork) -> None:
        super().__init__()
        self.network = network

    async def publish(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        self._dispatch(session_id, message)
        seq = message_seq(message)
        if seq is None:
            return
        for other in self.network.buses:
            if other is not self:
                other._dispatch(session_id, wake_message(session_id, seq))

    async def send_instance(self, instance_id: str, message: dict[str, Any]) -> None:
        raw = json.dumps(message, separators=(",", ":"))
        size = len(raw.encode("utf-8"))
        assert size <= NOTIFY_LIMIT, f"payload of {size} bytes is over the limit"
        self.network.sent.append((instance_id, message, size))
        if instance_id in self.network.dropped:
            return
        decoded = json.loads(raw)
        loop = asyncio.get_running_loop()
        for bus in self.network.buses:
            handler = bus._instances.get(instance_id)
            if handler is not None:
                loop.call_soon(handler, decoded)
