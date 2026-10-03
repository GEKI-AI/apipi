"""Worker-side outbox for protocol v2 durable envelopes.

The turn runtime reports results through a `ResultSink`; in split mode
the sink implementation appends durable v2 envelopes here instead of
writing to the database. Envelopes stay buffered until the API sends
the cumulative `ack{last_seq}`, so losing the socket (or the API)
loses nothing: the worker replays everything after `hello.reply` on
reconnect. See `specs/decisions/0015-worker-protocol-v2.md`.

The buffer is per session with a worker-assigned monotonic `seq`. It
is bounded by message count and total bytes; when it is full the
caller fails the turn with `worker_outbox_full`, except for a small
emergency budget reserved for that failure itself. An optional disk
spool (`APIPI_WORKER_OUTBOX_DIR`) keeps a write-through JSONL copy
per session so buffered envelopes survive a worker restart.
"""

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apipi.protocol import EnvelopePayload, WorkerEnvelope

log = logging.getLogger("apipi.worker")

EMERGENCY_BUDGET = 10


class OutboxFull(Exception):
    """The outbox is full; the turn cannot report more results."""

    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(str(session_id))
        self.session_id = session_id


@dataclass
class _SessionBuffer:
    issued: int = 0
    acked: int = 0
    envelopes: deque[dict[str, Any]] = field(default_factory=deque)
    bytes: int = 0
    emergency_used: int = 0


def envelope_size(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))


class Outbox:
    def __init__(
        self,
        *,
        max_messages: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        spool_dir: str | Path | None = None,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        if max_bytes < 1024:
            raise ValueError("max_bytes must be >= 1024")
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self.spool_dir = Path(spool_dir) if spool_dir is not None else None
        if self.spool_dir is not None:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[uuid.UUID, _SessionBuffer] = {}
        self._messages = 0
        self._bytes = 0
        self._dirty = asyncio.Event()

    def _buffer(self, session_id: uuid.UUID) -> _SessionBuffer:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            buffer = _SessionBuffer()
            self._sessions[session_id] = buffer
        return buffer

    def set_base(self, session_id: uuid.UUID, last_seq: int) -> None:
        """Record the API's persisted cursor without dropping anything.

        Entries at or below `last_seq` are already durable on the API
        and are pruned; the per-session `seq` counter continues past
        the highest seq ever issued.
        """
        buffer = self._buffer(session_id)
        while buffer.envelopes and int(buffer.envelopes[0]["seq"]) <= last_seq:
            dropped = buffer.envelopes.popleft()
            size = envelope_size(dropped)
            buffer.bytes -= size
            self._messages -= 1
            self._bytes -= size
        buffer.acked = max(buffer.acked, last_seq)
        buffer.issued = max(buffer.issued, last_seq)
        self._rewrite_spool(session_id)

    def high_water(self, session_id: uuid.UUID) -> int:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return 0
        return buffer.issued

    def acked_seq(self, session_id: uuid.UUID) -> int:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return 0
        return buffer.acked

    async def wait_acked(
        self, session_id: uuid.UUID, seq: int, *, timeout: float
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.acked_seq(session_id) < seq:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def append(
        self,
        session_id: uuid.UUID,
        type: str,
        payload: EnvelopePayload | dict[str, Any],
        *,
        turn_id: uuid.UUID | None = None,
        emergency: bool = False,
    ) -> dict[str, Any]:
        """Buffer one durable envelope and return it (with its `seq`).

        Raises OutboxFull when the bounds are hit. The emergency flag
        spends a small reserved budget so a full outbox can still
        report the `worker_outbox_full` failure itself.
        """
        buffer = self._buffer(session_id)
        seq = buffer.issued + 1
        envelope = WorkerEnvelope.build(
            session_id, seq, type, payload, turn_id=turn_id
        ).to_wire()
        size = envelope_size(envelope)
        over = (
            self._messages + 1 > self.max_messages
            or self._bytes + size > self.max_bytes
        )
        if over and not (emergency and buffer.emergency_used < EMERGENCY_BUDGET):
            raise OutboxFull(session_id)
        if over:
            buffer.emergency_used += 1
        buffer.issued = seq
        buffer.envelopes.append(envelope)
        buffer.bytes += size
        self._messages += 1
        self._bytes += size
        self._spool_append(session_id, envelope)
        self._dirty.set()
        return envelope

    def acked(self, session_id: uuid.UUID, last_seq: int) -> None:
        """Drop everything at or below the cumulative ack."""
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return
        while buffer.envelopes and int(buffer.envelopes[0]["seq"]) <= last_seq:
            dropped = buffer.envelopes.popleft()
            size = envelope_size(dropped)
            buffer.bytes -= size
            self._messages -= 1
            self._bytes -= size
        buffer.acked = max(buffer.acked, last_seq)
        self._rewrite_spool(session_id)

    def pending(
        self, session_id: uuid.UUID, *, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return []
        return [
            envelope
            for envelope in buffer.envelopes
            if int(envelope["seq"]) > after_seq
        ]

    def pending_sessions(self) -> list[uuid.UUID]:
        return [
            session_id
            for session_id, buffer in self._sessions.items()
            if buffer.envelopes
        ]

    def drop_session(self, session_id: uuid.UUID) -> None:
        buffer = self._sessions.pop(session_id, None)
        if buffer is None:
            self._remove_spool(session_id)
            return
        self._messages -= len(buffer.envelopes)
        self._bytes -= buffer.bytes
        self._remove_spool(session_id)

    def mark_dirty(self) -> None:
        """Wake the sender pump (e.g. after adopting hello cursors)."""
        self._dirty.set()

    async def wait_dirty(self, timeout: float | None = None) -> bool:
        if self._dirty.is_set():
            self._dirty.clear()
            return True
        if timeout is None:
            await self._dirty.wait()
            self._dirty.clear()
            return True
        try:
            await asyncio.wait_for(self._dirty.wait(), timeout=timeout)
        except TimeoutError:
            return False
        self._dirty.clear()
        return True

    def load_spool(self) -> dict[uuid.UUID, int]:
        """Reload spooled envelopes after a restart; returns high-waters."""
        if self.spool_dir is None:
            return {}
        waters: dict[uuid.UUID, int] = {}
        for path in sorted(self.spool_dir.glob("*.jsonl")):
            try:
                session_id = uuid.UUID(path.stem)
            except ValueError:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                log.warning(
                    "worker outbox spool unreadable",
                    extra={"event": "worker.outbox.spool_error", "path": str(path)},
                )
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict) or "seq" not in envelope:
                    continue
                buffer = self._buffer(session_id)
                seq = int(envelope["seq"])
                if seq <= buffer.issued:
                    continue
                buffer.issued = seq
                buffer.envelopes.append(envelope)
                size = envelope_size(envelope)
                buffer.bytes += size
                self._messages += 1
                self._bytes += size
            waters[session_id] = self._buffer(session_id).issued
        return waters

    def _spool_path(self, session_id: uuid.UUID) -> Path | None:
        if self.spool_dir is None:
            return None
        return self.spool_dir / f"{session_id}.jsonl"

    def _spool_append(self, session_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        path = self._spool_path(session_id)
        if path is None:
            return
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, separators=(",", ":")) + "\n")
                handle.flush()
        except OSError:
            log.warning(
                "worker outbox spool write failed",
                extra={"event": "worker.outbox.spool_error", "path": str(path)},
            )

    def _rewrite_spool(self, session_id: uuid.UUID) -> None:
        path = self._spool_path(session_id)
        if path is None:
            return
        buffer = self._sessions.get(session_id)
        try:
            if not buffer or not buffer.envelopes:
                path.unlink(missing_ok=True)
                return
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for envelope in buffer.envelopes:
                    handle.write(json.dumps(envelope, separators=(",", ":")) + "\n")
            tmp.replace(path)
        except OSError:
            log.warning(
                "worker outbox spool rewrite failed",
                extra={"event": "worker.outbox.spool_error", "path": str(path)},
            )

    def _remove_spool(self, session_id: uuid.UUID) -> None:
        path = self._spool_path(session_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "worker outbox spool remove failed",
                extra={"event": "worker.outbox.spool_error", "path": str(path)},
            )

    def describe(self) -> dict[str, Any]:
        return {
            "messages": self._messages,
            "bytes": self._bytes,
            "max_messages": self.max_messages,
            "max_bytes": self.max_bytes,
            "sessions": len(self._sessions),
            "spool_dir": str(self.spool_dir) if self.spool_dir is not None else None,
        }
