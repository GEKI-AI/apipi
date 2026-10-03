"""Worker-side outbox for protocol v2 durable envelopes.

The turn runtime reports results through a `ResultSink`; in split mode
the sink implementation appends durable v2 envelopes here instead of
writing to the database. Envelopes stay buffered until the API sends
the cumulative `ack{last_seq}`, so losing the socket (or the API)
loses nothing: the worker replays everything after `hello.reply` on
reconnect. See `specs/decisions/0015-worker-protocol-v2.md`.

The buffer is per session with a worker-assigned monotonic `seq`. It
is bounded by message count and total bytes, and one session may use
at most `SESSION_SHARE` of each bound. When a bound is hit the caller
fails the turn with `worker_outbox_full`, except for a small emergency
budget reserved for that failure itself. An envelope over
`MAX_MESSAGE_BYTES` fails the turn with `worker_message_too_large`.

Each session also has a "sent" mark: the pump sends an envelope once
per connection, and `begin_connection` resets the mark so a reconnect
resends everything that is still unacked. An optional disk spool
(`APIPI_WORKER_OUTBOX_DIR`) keeps an append-only JSONL file per
session so buffered envelopes survive a worker restart. Appends reach
the operating system at once, `maintain_spool` fsyncs the touched
files every `SPOOL_FSYNC_SECONDS` and compacts files whose acked
prefix grew, both off the event loop.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apipi.common.logutil import RateLimitedLog
from apipi.config import ConfigError
from apipi.protocol import (
    BASELINE_FEATURES,
    MAX_MESSAGE_BYTES,
    TYPE_FEATURES,
    EnvelopePayload,
    WorkerEnvelope,
    wire_size,
)

log = logging.getLogger("apipi.worker")

EMERGENCY_BUDGET = 10
HIGH_WATER_FRACTION = 0.8
SESSION_SHARE = 0.5
SPOOL_FSYNC_SECONDS = 1.0
SPOOL_COMPACT_MIN_LINES = 512
FLOOR_LIMIT = 4096


class OutboxFull(Exception):
    """The outbox is full; the turn cannot report more results."""

    code = "worker_outbox_full"
    message = "Worker outbox is full"

    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(str(session_id))
        self.session_id = session_id


class EnvelopeTooLarge(OutboxFull):
    """One envelope is over `MAX_MESSAGE_BYTES`; it can never be sent."""

    code = "worker_message_too_large"
    message = "A result is too large for the worker protocol"

    def __init__(self, session_id: uuid.UUID, size: int) -> None:
        super().__init__(session_id)
        self.size = size


class FeatureUnsupported(ConfigError):
    """The API did not advertise the feature an envelope type needs."""


@dataclass
class _SessionBuffer:
    issued: int = 0
    acked: int = 0
    envelopes: deque[dict[str, Any]] = field(default_factory=deque)
    added: deque[float] = field(default_factory=deque)
    bytes: int = 0
    emergency_used: int = 0
    sent: int = 0
    unsent: deque[dict[str, Any]] = field(default_factory=deque)
    spooled: int = 0
    dead: int = 0


def envelope_size(envelope: dict[str, Any]) -> int:
    return wire_size(envelope)


def _line(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, separators=(",", ":")) + "\n"


def _fsync_paths(paths: list[Path], directory: Path | None) -> None:
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
        except OSError:
            log.warning(
                "worker outbox spool fsync failed",
                extra={"event": "worker.outbox.spool_error", "path": str(path)},
            )
        finally:
            os.close(fd)
    if directory is not None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def _write_snapshot(path: Path, envelopes: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for envelope in envelopes:
            handle.write(_line(envelope))
        handle.flush()
        os.fsync(handle.fileno())


class Outbox:
    def __init__(
        self,
        *,
        max_messages: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        spool_dir: str | Path | None = None,
        metrics: Any | None = None,
        session_share: float = SESSION_SHARE,
        compact_min: int = SPOOL_COMPACT_MIN_LINES,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        if max_bytes < 1024:
            raise ValueError("max_bytes must be >= 1024")
        if not 0 < session_share <= 1:
            raise ValueError("session_share must be in (0, 1]")
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self.session_messages = max(1, int(max_messages * session_share))
        self.session_bytes = max(1, int(max_bytes * session_share))
        self.compact_min = compact_min
        self.metrics = metrics
        self.peer_features: frozenset[str] = BASELINE_FEATURES
        self._warnings = RateLimitedLog(log)
        self.spool_dir = Path(spool_dir) if spool_dir is not None else None
        if self.spool_dir is not None:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.spool_skipped = 0
        self._sessions: dict[uuid.UUID, _SessionBuffer] = {}
        self._floors: OrderedDict[uuid.UUID, int] = OrderedDict()
        self._unsynced: set[Path] = set()
        self._messages = 0
        self._bytes = 0
        self._dirty = asyncio.Event()

    def _buffer(self, session_id: uuid.UUID) -> _SessionBuffer:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            buffer = _SessionBuffer()
            floor = self._floors.pop(session_id, 0)
            buffer.issued = buffer.acked = buffer.sent = floor
            self._sessions[session_id] = buffer
        return buffer

    def set_base(self, session_id: uuid.UUID, last_seq: int) -> None:
        """Record the API's persisted cursor without dropping anything.

        Entries at or below `last_seq` are already durable on the API
        and are pruned; the per-session `seq` counter continues past
        the highest seq ever issued.
        """
        buffer = self._buffer(session_id)
        self._trim(buffer, last_seq, observe=False)
        buffer.acked = max(buffer.acked, last_seq)
        buffer.issued = max(buffer.issued, last_seq)
        buffer.sent = max(buffer.sent, buffer.acked)
        self._after_trim(session_id, buffer)

    def high_water(self, session_id: uuid.UUID) -> int:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return self._floors.get(session_id, 0)
        return buffer.issued

    def acked_seq(self, session_id: uuid.UUID) -> int:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return self._floors.get(session_id, 0)
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

        Raises OutboxFull when a bound is hit and EnvelopeTooLarge when
        the envelope can never be sent. The emergency flag spends a
        small reserved budget so a full outbox can still report the
        `worker_outbox_full` failure itself.
        """
        feature = TYPE_FEATURES.get(type)
        if feature is not None and feature not in self.peer_features:
            self._warnings.warning(
                "envelope type needs a feature the API did not advertise",
                event="worker.outbox.feature_missing",
                error_code="feature_unsupported",
                session_id=session_id,
                type=type,
                feature=feature,
            )
            raise FeatureUnsupported(
                f"the API does not support {feature}, so {type} cannot be sent"
            )
        buffer = self._buffer(session_id)
        seq = buffer.issued + 1
        envelope = WorkerEnvelope.build(
            session_id, seq, type, payload, turn_id=turn_id
        ).to_wire()
        size = envelope_size(envelope)
        if size > MAX_MESSAGE_BYTES:
            self._warnings.warning(
                "worker envelope too large",
                event="worker.outbox.oversize",
                error_code="worker_message_too_large",
                session_id=session_id,
                type=type,
                size=size,
                limit=MAX_MESSAGE_BYTES,
            )
            raise EnvelopeTooLarge(session_id, size)
        over_worker = (
            self._messages + 1 > self.max_messages
            or self._bytes + size > self.max_bytes
        )
        over_session = (
            len(buffer.envelopes) + 1 > self.session_messages
            or buffer.bytes + size > self.session_bytes
        )
        over = over_worker or over_session
        if over and not (emergency and buffer.emergency_used < EMERGENCY_BUDGET):
            self._warnings.warning(
                "worker outbox full",
                event="worker.outbox.full",
                error_code="worker_outbox_full",
                session_id=session_id,
                scope="worker" if over_worker else "session",
                messages=self._messages,
                bytes=self._bytes,
                session_messages=len(buffer.envelopes),
                session_bytes=buffer.bytes,
            )
            raise OutboxFull(session_id)
        if over:
            buffer.emergency_used += 1
        buffer.issued = seq
        buffer.envelopes.append(envelope)
        buffer.unsent.append(envelope)
        buffer.added.append(time.monotonic())
        buffer.bytes += size
        self._messages += 1
        self._bytes += size
        self._spool_append(session_id, buffer, envelope)
        self._dirty.set()
        if (
            self._messages >= self.max_messages * HIGH_WATER_FRACTION
            or self._bytes >= self.max_bytes * HIGH_WATER_FRACTION
        ):
            self._warnings.warning(
                "worker outbox above 80 percent",
                event="worker.outbox.high",
                error_code="worker_outbox_high",
                messages=self._messages,
                max_messages=self.max_messages,
                bytes=self._bytes,
                max_bytes=self.max_bytes,
            )
        return envelope

    def acked(self, session_id: uuid.UUID, last_seq: int) -> None:
        """Drop everything at or below the cumulative ack."""
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return
        self._trim(buffer, last_seq, observe=True)
        buffer.acked = max(buffer.acked, last_seq)
        buffer.sent = max(buffer.sent, buffer.acked)
        self._after_trim(session_id, buffer)

    def _trim(self, buffer: _SessionBuffer, last_seq: int, *, observe: bool) -> None:
        now = time.monotonic()
        while buffer.envelopes and int(buffer.envelopes[0]["seq"]) <= last_seq:
            dropped = buffer.envelopes.popleft()
            added = buffer.added.popleft() if buffer.added else now
            size = envelope_size(dropped)
            buffer.bytes -= size
            buffer.dead += 1
            self._messages -= 1
            self._bytes -= size
            if observe and self.metrics is not None:
                self.metrics.observe_worker_ack(now - added)
        while buffer.unsent and int(buffer.unsent[0]["seq"]) <= last_seq:
            buffer.unsent.popleft()

    def _after_trim(self, session_id: uuid.UUID, buffer: _SessionBuffer) -> None:
        if not buffer.envelopes and buffer.spooled:
            self._remove_spool(session_id)
            buffer.spooled = 0
            buffer.dead = 0

    def begin_connection(self) -> int:
        """Start over on a new socket: everything unacked is sent again.

        Returns how many envelopes were sent before and are sent again.
        """
        resent = 0
        for buffer in self._sessions.values():
            for envelope in buffer.envelopes:
                if int(envelope["seq"]) > buffer.sent:
                    break
                resent += 1
            buffer.sent = buffer.acked
            buffer.unsent = deque(buffer.envelopes)
        self._dirty.set()
        return resent

    def unsent_sessions(self) -> list[uuid.UUID]:
        return [
            session_id for session_id, buffer in self._sessions.items() if buffer.unsent
        ]

    def next_unsent(self, session_id: uuid.UUID) -> dict[str, Any] | None:
        buffer = self._sessions.get(session_id)
        if buffer is None or not buffer.unsent:
            return None
        return buffer.unsent[0]

    def mark_sent(self, session_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        buffer = self._sessions.get(session_id)
        if buffer is None:
            return
        if buffer.unsent and buffer.unsent[0] is envelope:
            buffer.unsent.popleft()
        buffer.sent = max(buffer.sent, int(envelope["seq"]))

    def spool_size(self) -> int:
        if self.spool_dir is None:
            return 0
        total = 0
        for path in self.spool_dir.glob("*.jsonl"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def oldest_seconds(self) -> float:
        """Age of the oldest envelope that is not acked yet."""
        firsts = [buffer.added[0] for buffer in self._sessions.values() if buffer.added]
        return time.monotonic() - min(firsts) if firsts else 0.0

    def observe(self) -> None:
        """Publish the outbox gauges."""
        if self.metrics is not None:
            self.metrics.set_worker_outbox(
                messages=self._messages,
                size=self._bytes,
                oldest_seconds=self.oldest_seconds(),
            )
            if self.spool_dir is not None:
                self.metrics.set_worker_spool_bytes(self.spool_size())

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

    def release(self, session_id: uuid.UUID) -> None:
        """Forget the buffer of an ended session once nothing is unacked.

        The sequence counter is remembered (for a bounded number of
        sessions), so a later append continues the numbering.
        """
        buffer = self._sessions.get(session_id)
        if buffer is None or buffer.envelopes:
            return
        del self._sessions[session_id]
        self._floors[session_id] = buffer.issued
        self._floors.move_to_end(session_id)
        while len(self._floors) > FLOOR_LIMIT:
            self._floors.popitem(last=False)
        if buffer.spooled:
            self._remove_spool(session_id)

    def drop_session(self, session_id: uuid.UUID) -> None:
        self._floors.pop(session_id, None)
        buffer = self._sessions.pop(session_id, None)
        if buffer is not None:
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
        """Reload spooled envelopes after a restart; returns high-waters.

        Reloaded envelopes count as sent before, so the first pump
        round after the next connect is a replay.
        """
        if self.spool_dir is None:
            return {}
        for stale in self.spool_dir.glob("*.tmp"):
            with contextlib.suppress(OSError):
                stale.unlink()
        waters: dict[uuid.UUID, int] = {}
        for path in sorted(self.spool_dir.glob("*.jsonl")):
            try:
                session_id = uuid.UUID(path.stem)
            except ValueError:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                log.warning(
                    "worker outbox spool unreadable",
                    extra={"event": "worker.outbox.spool_error", "path": str(path)},
                )
                continue
            buffer = self._buffer(session_id)
            for line in lines:
                if not line.strip():
                    continue
                try:
                    envelope = json.loads(line)
                    seq = int(envelope["seq"])
                except (ValueError, KeyError, TypeError):
                    self.spool_skipped += 1
                    continue
                buffer.spooled += 1
                if seq <= buffer.issued:
                    buffer.dead += 1
                    continue
                buffer.issued = seq
                buffer.envelopes.append(envelope)
                buffer.unsent.append(envelope)
                buffer.added.append(time.monotonic())
                size = envelope_size(envelope)
                buffer.bytes += size
                self._messages += 1
                self._bytes += size
            buffer.sent = buffer.issued
            waters[session_id] = buffer.issued
        return waters

    def _spool_path(self, session_id: uuid.UUID) -> Path | None:
        if self.spool_dir is None:
            return None
        return self.spool_dir / f"{session_id}.jsonl"

    def _spool_append(
        self, session_id: uuid.UUID, buffer: _SessionBuffer, envelope: dict[str, Any]
    ) -> None:
        path = self._spool_path(session_id)
        if path is None:
            return
        started = time.monotonic()
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_line(envelope))
        except OSError:
            self._warnings.warning(
                "worker outbox spool write failed",
                event="worker.outbox.spool_error",
                error_code="spool_error",
                path=str(path),
            )
            return
        buffer.spooled += 1
        self._unsynced.add(path)
        if self.metrics is not None:
            self.metrics.observe_worker_spool_write(time.monotonic() - started)

    def sync_spool(self) -> None:
        """Fsync every touched spool file now (shutdown)."""
        paths = list(self._unsynced)
        self._unsynced.clear()
        if paths:
            _fsync_paths(paths, self.spool_dir)

    async def maintain_spool(self) -> None:
        """Fsync the touched spool files and compact the ones with a long acked prefix.

        Runs every `SPOOL_FSYNC_SECONDS`. The disk work happens in a
        thread, so the event loop only pays for small appends.
        """
        if self.spool_dir is None:
            return
        paths = list(self._unsynced)
        self._unsynced.clear()
        if paths:
            await asyncio.to_thread(_fsync_paths, paths, self.spool_dir)
        for session_id, buffer in list(self._sessions.items()):
            if buffer.dead >= self.compact_min and buffer.dead >= len(buffer.envelopes):
                await self._compact(session_id, buffer)

    async def _compact(self, session_id: uuid.UUID, buffer: _SessionBuffer) -> None:
        path = self._spool_path(session_id)
        snapshot = list(buffer.envelopes)
        if path is None or not snapshot:
            return
        started = time.monotonic()
        last = int(snapshot[-1]["seq"])
        tmp = path.with_suffix(".tmp")
        try:
            await asyncio.to_thread(_write_snapshot, tmp, snapshot)
        except OSError:
            self._compaction_failed(path, tmp)
            return
        try:
            if self._sessions.get(session_id) is not buffer or not buffer.envelopes:
                tmp.unlink(missing_ok=True)
                return
            newer = [e for e in buffer.envelopes if int(e["seq"]) > last]
            with tmp.open("a", encoding="utf-8") as handle:
                for envelope in newer:
                    handle.write(_line(envelope))
            tmp.replace(path)
        except OSError:
            self._compaction_failed(path, tmp)
            return
        first = int(buffer.envelopes[0]["seq"])
        buffer.spooled = len(snapshot) + len(newer)
        buffer.dead = sum(1 for e in snapshot if int(e["seq"]) < first)
        if newer:
            self._unsynced.add(path)
        if self.metrics is not None:
            self.metrics.observe_worker_spool_write(time.monotonic() - started)

    def _compaction_failed(self, path: Path, tmp: Path) -> None:
        self._warnings.warning(
            "worker outbox spool compaction failed",
            event="worker.outbox.spool_error",
            error_code="spool_error",
            path=str(path),
        )
        with contextlib.suppress(OSError):
            tmp.unlink()

    def _remove_spool(self, session_id: uuid.UUID) -> None:
        path = self._spool_path(session_id)
        if path is None:
            return
        self._unsynced.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self._warnings.warning(
                "worker outbox spool remove failed",
                event="worker.outbox.spool_error",
                error_code="spool_error",
                path=str(path),
            )

    def describe(self) -> dict[str, Any]:
        return {
            "messages": self._messages,
            "bytes": self._bytes,
            "max_messages": self.max_messages,
            "max_bytes": self.max_bytes,
            "session_messages": self.session_messages,
            "session_bytes": self.session_bytes,
            "sessions": len(self._sessions),
            "spool_dir": str(self.spool_dir) if self.spool_dir is not None else None,
        }
