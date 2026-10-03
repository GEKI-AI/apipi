"""The in-process event bus: per-session queues and turn abort events."""

import asyncio
import time
import uuid
from typing import Any, Protocol

from apipi.common.errors import ApiError


def wake_message(session_id: uuid.UUID, seq: int) -> dict[str, Any]:
    return {
        "kind": "wake",
        "session_id": str(session_id),
        "seq": int(seq),
        "published_at": time.time(),
    }


def live_event_body(
    session_id: uuid.UUID, *, type: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "type": type,
        "session_id": str(session_id),
        "data": data if data is not None else {},
    }


def is_wake(message: dict[str, Any]) -> bool:
    return message.get("kind") == "wake"


def message_seq(message: dict[str, Any]) -> int | None:
    seq = message.get("seq")
    return int(seq) if isinstance(seq, (int, float)) else None


class EventBus(Protocol):
    """Fan-out for session events. ``publish`` never raises for remote
    delivery failures; durable wakes are covered by the fallback poll."""

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        """Return the local queue receiving this session's messages."""
        ...

    def unsubscribe(
        self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]
    ) -> None: ...

    async def publish(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        """Fan out one message to local subscribers and other replicas."""
        ...

    def watch_turn(self, session_id: uuid.UUID) -> asyncio.Event: ...
    def turn_abort(self, session_id: uuid.UUID) -> asyncio.Event | None: ...
    def unwatch_turn(self, session_id: uuid.UUID) -> None: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...


class LocalFanout:
    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._abort: dict[uuid.UUID, asyncio.Event] = {}

    def watch_turn(self, session_id: uuid.UUID) -> asyncio.Event:
        ev = asyncio.Event()
        self._abort[session_id] = ev
        return ev

    def turn_abort(self, session_id: uuid.UUID) -> asyncio.Event | None:
        return self._abort.get(session_id)

    def unwatch_turn(self, session_id: uuid.UUID) -> None:
        self._abort.pop(session_id, None)

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(
        self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        subs = self._subs.get(session_id)
        if subs is None:
            return
        if queue in subs:
            subs.remove(queue)
        if not subs:
            del self._subs[session_id]

    def _dispatch(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        for queue in list(self._subs.get(session_id, ())):
            queue.put_nowait(message)


class InMemoryEventBus(LocalFanout):
    """Single-process fan-out with the previous EventHub semantics."""

    async def publish(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        self._dispatch(session_id, message)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


EventHub = InMemoryEventBus


def request_cancel(
    hub: EventBus, session_id: uuid.UUID, *, status: str
) -> asyncio.Event | None:
    abort = hub.turn_abort(session_id)
    if status != "in_progress" and abort is None:
        raise ApiError(
            "invalid_request",
            "Session is not in_progress",
            code="invalid_request",
        )
    return abort
