"""Live delta relay from the worker to the API (issue #445).

In split mode the worker runs the turn, so the model text fragments
surface in the worker process. The worker does not publish them to the
event bus itself: it coalesces them over :data:`DELTA_WINDOW` seconds
per session and sends them as ephemeral v2 envelopes
(``delta.text``) over the worker WebSocket. The API validates each
envelope and publishes it as a ``live`` bus message, so SSE clients on
any replica see token streaming. Delivery is at-most-once: deltas are
never persisted and never acked, and the final item stays the source
of truth.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from apipi.common.event_bus import EventBus
from apipi.protocol import PAYLOAD_MODELS, WorkerEnvelope

log = logging.getLogger("apipi.worker")

DELTA_WINDOW = 0.04
DELTA_MAX_TEXT = 4000
DELTA_PUBLIC_TYPE = "agent.session.turn.output_text.delta"

SendEnvelope = Callable[[dict[str, Any]], Awaitable[None]]


class DeltaRelay:
    """Coalesce text fragments into ephemeral v2 envelopes.

    Fragments submitted close together (within ``window`` seconds) for
    the same session and turn leave in one ``delta.text`` envelope, so
    a fast model does not send one socket message per token. Buffers
    over ``max_text`` characters are split into several envelopes, so
    each envelope stays well under the NOTIFY payload limit after the
    API fans it out. ``seq`` is monotonic per session.
    """

    def __init__(
        self,
        send: SendEnvelope | None = None,
        *,
        window: float = DELTA_WINDOW,
        max_text: int = DELTA_MAX_TEXT,
    ) -> None:
        self._send = send
        self._window = window
        self._max_text = max_text
        self._buffers: dict[tuple[uuid.UUID, uuid.UUID, str], list[str]] = {}
        self._seq: dict[uuid.UUID, int] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.dropped = 0

    def attach(self, send: SendEnvelope) -> None:
        """Set the socket sender once the worker is connected."""
        self._send = send

    def detach(self) -> None:
        """Drop the socket sender when the connection closes."""
        self._send = None

    def forget(self, session_id: uuid.UUID) -> None:
        """Drop buffered fragments and the seq counter for a session."""
        self._buffers = {
            key: parts for key, parts in self._buffers.items() if key[0] != session_id
        }
        self._seq.pop(session_id, None)

    async def submit(
        self,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        text: str,
        *,
        kind: str = "delta.text",
    ) -> None:
        """Buffer one text fragment. Empty fragments are dropped."""
        if not text:
            return
        async with self._lock:
            key = (session_id, turn_id, kind)
            self._buffers.setdefault(key, []).append(text)
            if self._flush_task is None or self._flush_task.done():
                loop = asyncio.get_running_loop()
                self._flush_task = loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self._window)
        try:
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("delta relay flush failed; dropping batch")

    async def flush(self) -> None:
        """Send one envelope per buffered session and turn."""
        async with self._lock:
            pending = self._buffers
            self._buffers = {}
        for (session_id, turn_id, kind), parts in pending.items():
            joined = "".join(parts)
            if not joined:
                continue
            for chunk in _split(joined, self._max_text):
                await self._emit(session_id, turn_id, chunk, kind=kind)

    async def _emit(
        self,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        text: str,
        *,
        kind: str,
    ) -> None:
        send = self._send
        if send is None:
            log.debug("delta relay has no sender; dropping fragment")
            self.dropped += 1
            return
        last = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = last
        try:
            payload = PAYLOAD_MODELS[kind].model_validate(
                {"turn_id": turn_id, "text": text}
            )
            await send(
                WorkerEnvelope.build(
                    session_id, last, kind, payload, turn_id=turn_id
                ).to_wire()
            )
        except Exception:
            # At-most-once: a dead socket drops the batch. Never let
            # a send error escape into the turn via the flush task.
            log.debug("delta relay send failed; dropping fragment")
            self.dropped += 1


def _split(text: str, limit: int) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


class LiveRedirectBus:
    """An :class:`EventBus` that reroutes live deltas to the socket.

    The worker builds its execution with this bus wrapped around the
    configured bus. ``output_text.delta`` fragments go to the relay, so
    they travel over the worker socket and the API fans them out.
    Without a relay live messages are published directly.
    """

    def __init__(self, inner: EventBus, relay: DeltaRelay | None = None) -> None:
        self._inner = inner
        self._relay = relay

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        return self._inner.subscribe(session_id)

    def unsubscribe(
        self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        self._inner.unsubscribe(session_id, queue)

    def watch_turn(self, session_id: uuid.UUID) -> asyncio.Event:
        return self._inner.watch_turn(session_id)

    def turn_abort(self, session_id: uuid.UUID) -> asyncio.Event | None:
        return self._inner.turn_abort(session_id)

    def unwatch_turn(self, session_id: uuid.UUID) -> None:
        self._inner.unwatch_turn(session_id)

    async def start(self) -> None:
        await self._inner.start()

    async def close(self) -> None:
        await self._inner.close()

    async def publish(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        relay = self._relay
        if (
            relay is not None
            and message.get("type") == DELTA_PUBLIC_TYPE
            and message.get("seq") is None
        ):
            data = message.get("data")
            if not isinstance(data, dict):
                return
            delta = data.get("delta")
            raw_turn = data.get("turn_id")
            if not isinstance(delta, str) or not delta:
                return
            try:
                turn_id = uuid.UUID(str(raw_turn))
            except (ValueError, TypeError, AttributeError):
                return
            await relay.submit(session_id, turn_id, delta)
            return
        await self._inner.publish(session_id, message)

    @property
    def inner(self) -> EventBus:
        """The wrapped bus, for loops that must bypass the relay."""
        return self._inner


def live_event_data(message: dict[str, Any]) -> tuple[uuid.UUID, str] | None:
    """Split a relayed ``delta.text`` payload into turn id and text."""
    if message.get("type") not in {"delta.text", "delta.reasoning"}:
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    try:
        turn_id = uuid.UUID(str(payload.get("turn_id")))
    except (ValueError, TypeError, AttributeError):
        return None
    return turn_id, text


def _now() -> float:
    return time.monotonic()
