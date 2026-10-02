"""API-side ingest for worker protocol v2 durable envelopes.

The worker buffers durable envelopes in its outbox until the API
sends the cumulative `ack{last_seq}`. Ingest runs per worker
connection: envelopes are batched (about 50ms or N messages) and
applied in one transaction per batch, then acked, then fanned out
over the `EventBus` so SSE wakes without polling.

Idempotency comes from the `worker_ingest` ledger
(`UNIQUE(session_id, worker_seq)`): a duplicate claim skips the
apply but still advances the cumulative ack. The cursor itself lives
on `sessions.worker_seq` and is reported in `hello.reply`, so a
reconnect replays exactly what is missing.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.gateway.logutil import log_event
from apipi.store.engine import Store
from apipi.store.models import SessionRow, Turn, WorkerIngest, utc_now
from apipi.worker.protocol import (
    EPHEMERAL_MESSAGE_TYPES,
    MAX_MESSAGE_BYTES,
    UnknownMessageType,
    WorkerEnvelope,
    parse_envelope,
)

log = logging.getLogger("apipi.worker")

NOT_LEASED = "not_leased"
TURN_MISMATCH = "turn_mismatch"
UNKNOWN_TURN = "unknown_turn"
UNKNOWN_EVENT = "unknown_event"
LIVE_EVENT = "live_event"
NOT_IMPLEMENTED = "not_implemented"
OVERSIZE = "oversize"
INVALID_ENVELOPE = "invalid_envelope"
UNKNOWN_SESSION = "unknown_session"

DEFERRED_TYPES = frozenset({"sandbox.status"})


@dataclass
class QueuedEnvelope:
    envelope: WorkerEnvelope
    raw_size: int


@dataclass
class IngestOutcome:
    acks: dict[uuid.UUID, int] = field(default_factory=dict)
    wakes: list[tuple[uuid.UUID, dict[str, Any]]] = field(default_factory=list)
    rejected: list[tuple[uuid.UUID, int, str]] = field(default_factory=list)
    presign_replies: list[dict[str, Any]] = field(default_factory=list)


class IngestBatcher:
    """Collect envelopes for one worker connection until flush time."""

    def __init__(
        self, *, max_messages: int = 100, max_bytes: int = 8 * 1024 * 1024
    ) -> None:
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._queued: list[QueuedEnvelope] = []
        self._bytes = 0
        self._first_at: float | None = None

    def add(self, envelope: WorkerEnvelope, raw_size: int) -> None:
        if self._first_at is None:
            self._first_at = time.monotonic()
        self._queued.append(QueuedEnvelope(envelope, raw_size))
        self._bytes += raw_size

    def __len__(self) -> int:
        return len(self._queued)

    def full(self) -> bool:
        return len(self._queued) >= self.max_messages or self._bytes >= self.max_bytes

    def window_expired(self, window_s: float) -> bool:
        return (
            self._first_at is not None and time.monotonic() - self._first_at >= window_s
        )

    def poll_timeout(self, window_s: float) -> float | None:
        """How long the socket loop waits before flushing a partial batch."""
        if not self._queued or self._first_at is None:
            return None
        return max(window_s - (time.monotonic() - self._first_at), 0.0)

    def should_flush(self, window_s: float) -> bool:
        return bool(self._queued) and (self.full() or self.window_expired(window_s))

    def take(self) -> list[QueuedEnvelope]:
        queued, self._queued = self._queued, []
        self._bytes = 0
        self._first_at = None
        return queued


def envelope_turn_id(envelope: WorkerEnvelope) -> uuid.UUID | None:
    if envelope.turn_id is not None:
        return envelope.turn_id
    payload = envelope.payload
    for key in ("turn_id",):
        raw = payload.get(key)
        if isinstance(raw, str) and raw:
            try:
                return uuid.UUID(raw)
            except ValueError:
                return None
    if envelope.type == "event":
        data = payload.get("data")
        if isinstance(data, dict):
            raw = data.get("turn_id")
            if isinstance(raw, str) and raw:
                try:
                    return uuid.UUID(raw)
                except ValueError:
                    return None
    return None


class _TurnCache:
    """Per-batch cache of turn lookups, one `LIMIT 1` read per session.

    A batch applies envelopes in order and only `turn.status` changes
    turn rows, so entries are dropped when one applies.
    """

    def __init__(self) -> None:
        self._running: dict[uuid.UUID, Turn | None] = {}
        self._latest: dict[uuid.UUID, Turn | None] = {}

    async def running(
        self, db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> Turn | None:
        from apipi.store.repo import get_running_turn

        if session_id not in self._running:
            self._running[session_id] = await get_running_turn(
                db, tenant_id, session_id
            )
        return self._running[session_id]

    async def latest(
        self, db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> Turn | None:
        from apipi.store.repo import get_latest_turn

        if session_id not in self._latest:
            self._latest[session_id] = await get_latest_turn(db, tenant_id, session_id)
        return self._latest[session_id]

    async def running_id(
        self, db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> uuid.UUID | None:
        turn = await self.running(db, tenant_id, session_id)
        return turn.id if turn is not None else None

    async def latest_id(
        self, db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The newest turn, running or already finished.

        Terminal envelopes (`turn.status` completion, `usage`, and the
        turn's public events) arrive after the row leaves `in_progress`,
        in the order the runtime emits them, so they bind to the latest
        turn instead of the running one.
        """
        turn = await self.latest(db, tenant_id, session_id)
        return turn.id if turn is not None else None

    def invalidate(self, session_id: uuid.UUID) -> None:
        self._running.pop(session_id, None)
        self._latest.pop(session_id, None)


async def _validate(
    db: AsyncSession,
    envelope: WorkerEnvelope,
    raw_size: int,
    *,
    worker_id: uuid.UUID,
    row: SessionRow | None,
    turn_cache: _TurnCache,
) -> str | None:
    """Return a reject reason, or None when the envelope may apply."""
    if raw_size > MAX_MESSAGE_BYTES:
        return OVERSIZE
    if row is None or row.worker_id != worker_id or row.lease_id is None:
        return NOT_LEASED
    if envelope.type in DEFERRED_TYPES:
        return NOT_IMPLEMENTED
    if envelope.type == "artifact.presign":
        raw_request = envelope.payload.get("request_id")
        if not isinstance(raw_request, str) or not raw_request:
            return INVALID_ENVELOPE
        try:
            uuid.UUID(raw_request)
        except ValueError:
            return INVALID_ENVELOPE
        raw_declared = envelope.payload.get("size")
        if not isinstance(raw_declared, int) or raw_declared < 1:
            return INVALID_ENVELOPE
        return None
    if envelope.type == "artifact.completed":
        payload = envelope.payload
        has_upload = isinstance(payload.get("upload_id"), str)
        has_artifact = isinstance(payload.get("artifact_id"), str)
        has_path = isinstance(payload.get("path"), str)
        if not (has_upload or has_artifact or has_path):
            return INVALID_ENVELOPE
        return None
    if envelope.type == "event":
        from apipi.services.sink import LIVE_EVENT_TYPES, PUBLIC_EVENT_TYPES

        inner = envelope.payload.get("type")
        if not isinstance(inner, str) or inner not in PUBLIC_EVENT_TYPES:
            return UNKNOWN_EVENT
        if inner in LIVE_EVENT_TYPES:
            return LIVE_EVENT
    turn_id = envelope_turn_id(envelope)
    if envelope.type in {"item.added", "item.done", "usage"} and turn_id is None:
        return INVALID_ENVELOPE
    if envelope.type == "turn.status" and envelope.payload.get("status") == "started":
        if turn_id is None:
            return INVALID_ENVELOPE
        running = await turn_cache.running_id(db, row.tenant_id, row.id)
        if running is not None and running != turn_id:
            return TURN_MISMATCH
        return None
    if envelope.type == "item.added":
        running = await turn_cache.running_id(db, row.tenant_id, row.id)
        if running is None or running != turn_id:
            return TURN_MISMATCH
        return None
    if turn_id is not None:
        latest = await turn_cache.latest_id(db, row.tenant_id, row.id)
        if latest is None or latest != turn_id:
            return TURN_MISMATCH
    if envelope.type == "session.status":
        status = envelope.payload.get("status")
        actions = envelope.payload.get("required_actions", [])
        if status is None and not actions:
            return INVALID_ENVELOPE
    return None


async def _claim(db: AsyncSession, session_id: uuid.UUID, seq: int, type: str) -> None:
    """Insert the ledger row; raises _Duplicate when already applied.

    Runs inside the caller's per-envelope savepoint: a duplicate rolls
    the savepoint back (nothing to keep), and so does any failure.
    """
    try:
        db.add(WorkerIngest(session_id=session_id, worker_seq=seq, envelope_type=type))
        await db.flush()
    except IntegrityError as exc:
        raise _Duplicate() from exc


async def _store_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    from apipi.store.repo import append_event

    event = await append_event(db, tenant_id, session_id, type=type, data=data)
    from apipi.services.sink import event_body

    return event_body(event)


async def _apply(
    db: AsyncSession,
    bus: Any,
    envelope: WorkerEnvelope,
    row: SessionRow,
    *,
    settings: Any,
    metrics: Any,
    wakes: list[tuple[uuid.UUID, dict[str, Any]]],
    turn_cache: _TurnCache,
    presign_replies: list[dict[str, Any]] | None = None,
    objects: Any | None = None,
) -> None:
    from apipi.services.failures import failure_from_dict
    from apipi.services.usage import usage_from
    from apipi.store.repo import (
        create_item,
        create_turn,
        get_item,
        get_session_turn,
        get_turn,
        update_session,
    )

    del bus
    tenant_id = row.tenant_id
    session_id = row.id
    payload = envelope.payload
    if envelope.type == "turn.status":
        status = payload["status"]
        turn_id = uuid.UUID(str(payload["turn_id"]))
        if status == "started":
            existing = await get_turn(db, tenant_id, turn_id)
            if existing is None or existing.session_id != session_id:
                await create_turn(
                    db,
                    tenant_id,
                    session_id,
                    status="in_progress",
                    turn_id=turn_id,
                )
        else:
            turn = await get_session_turn(db, tenant_id, session_id, turn_id)
            if turn is None:
                raise _Reject(UNKNOWN_TURN)
            turn.status = status
            turn.updated_at = utc_now()
            await db.flush()
        turn_cache.invalidate(session_id)
        return
    if envelope.type == "item.added":
        item_id = uuid.UUID(str(payload["item_id"]))
        raw_turn = payload.get("turn_id")
        turn_id = uuid.UUID(str(raw_turn)) if isinstance(raw_turn, str) else None
        existing = await get_item(db, tenant_id, item_id)
        if existing is not None:
            return
        # The row only: the runtime reports the added/done public events
        # as separate `event` envelopes, mirroring `_emit_item` one to one.
        await create_item(
            db,
            tenant_id,
            session_id,
            type=str(payload.get("item_type") or "message"),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            turn_id=turn_id,
            item_id=item_id,
        )
        return
    if envelope.type == "item.done":
        # Like `item.added`, this carries no public event: the runtime
        # reports item events as `event` envelopes. It only merges
        # carried data into the row, if any.
        item_id = uuid.UUID(str(payload["item_id"]))
        item = await get_item(db, tenant_id, item_id)
        if item is None or item.session_id != session_id:
            raise _Reject(UNKNOWN_TURN)
        raw_turn = payload.get("turn_id")
        if isinstance(raw_turn, str) and raw_turn:
            try:
                payload_turn = uuid.UUID(raw_turn)
            except ValueError:
                raise _Reject(TURN_MISMATCH) from None
            if item.turn_id is not None and payload_turn != item.turn_id:
                raise _Reject(TURN_MISMATCH)
        raw_data = payload.get("data")
        if isinstance(raw_data, dict) and raw_data:
            item.data = {**item.data, **raw_data}
            await db.flush()
        return
    if envelope.type == "usage":
        from apipi.services.runtime import _write_turn_log

        turn_id = uuid.UUID(str(payload["turn_id"]))
        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        if turn is None:
            raise _Reject(UNKNOWN_TURN)
        stored = usage_from(
            {
                "prompt_tokens": payload.get("prompt_tokens", 0),
                "completion_tokens": payload.get("completion_tokens", 0),
                "cache_read_tokens": payload.get("cache_read_tokens", 0),
                "cache_write_tokens": payload.get("cache_write_tokens", 0),
                "total_tokens": payload.get("total_tokens"),
            }
        )
        turn.usage = stored
        turn.updated_at = utc_now()
        await db.flush()
        raw_failure = payload.get("failure")
        failure = (
            failure_from_dict(raw_failure) if isinstance(raw_failure, dict) else None
        )
        await _write_turn_log(
            db,
            tenant_id,
            session_id,
            turn_id,
            status=str(payload.get("status") or "completed"),
            usage=stored,
            error_code=payload.get("error_code")
            if isinstance(payload.get("error_code"), str)
            else None,
            request_id=payload.get("request_id")
            if isinstance(payload.get("request_id"), str)
            else None,
            metrics=metrics,
            tracing=None,
            settings=settings,
            artifact_bytes=int(payload.get("artifact_bytes") or 0),
            user_id=payload.get("user_id")
            if isinstance(payload.get("user_id"), str)
            else None,
            failure=failure,
            tool_names=list(payload.get("tool_names") or []),
            tool_counts=dict(payload.get("tool_counts") or {}),
            mcp_names=list(payload.get("mcp_names") or []),
            mcp_counts=dict(payload.get("mcp_counts") or {}),
        )
        return
    if envelope.type == "event":
        inner = payload["type"]
        data = payload.get("data")
        wakes.append(
            (
                session_id,
                await _store_event(
                    db,
                    tenant_id,
                    session_id,
                    type=inner,
                    data=data if isinstance(data, dict) else {},
                ),
            )
        )
        return
    if envelope.type == "session.status":
        changes: dict[str, Any] = {}
        if payload.get("status") is not None:
            changes["status"] = payload["status"]
        if payload.get("required_actions") is not None:
            changes["required_actions"] = payload["required_actions"]
        await update_session(db, tenant_id, session_id, changes=changes)
        return
    if envelope.type == "artifact.presign":
        from apipi.services.worker_artifacts import issue_artifact_presign
        from apipi.store.blobs import object_store as _object_store

        request_id = uuid.UUID(str(payload["request_id"]))
        kind = str(payload.get("kind") or "artifact")
        filename = payload.get("filename")
        content_type = payload.get("content_type")
        raw_size = payload.get("size")
        if not isinstance(raw_size, int):
            raise _Reject(INVALID_ENVELOPE)
        size = raw_size
        sha256 = payload.get("sha256")
        replies = presign_replies if presign_replies is not None else []
        try:
            blobs = _blobs_for(settings, objects)
            used = await blobs.used_bytes(tenant_id, row.key_id, session_id)
            issued = await issue_artifact_presign(
                db,
                settings,
                tenant_id,
                session_id,
                kind=kind,
                filename=filename if isinstance(filename, str) else None,
                content_type=content_type if isinstance(content_type, str) else None,
                size=size,
                sha256=sha256 if isinstance(sha256, str) else None,
                key_id=row.key_id,
                used_bytes=used,
                objects=objects if objects is not None else _object_store(settings),
            )
        except Exception as exc:
            code, message = _artifact_error(exc)
            replies.append(
                {
                    "type": "artifact.presign.reply",
                    "session_id": str(session_id),
                    "request_id": str(request_id),
                    "ok": False,
                    "code": code,
                    "message": message,
                }
            )
            raise _Reject(code) from exc
        replies.append(
            {
                "type": "artifact.presign.reply",
                "session_id": str(session_id),
                "request_id": str(request_id),
                "ok": True,
                "upload_id": str(issued["upload_id"]),
                "url": issued.get("url"),
                "headers": issued.get("headers") or {},
                "expires_at": issued.get("expires_at"),
            }
        )
        return
    if envelope.type == "artifact.completed":
        from apipi.services.worker_artifacts import complete_artifact_upload
        from apipi.store.blobs import object_store as _object_store2

        raw_upload = payload.get("upload_id")
        raw_path = payload.get("path")
        raw_size = payload.get("size_bytes", payload.get("size"))
        sha256 = payload.get("sha256")
        name = payload.get("name")
        raw_turn = payload.get("turn_id")
        turn_id = None
        if isinstance(raw_turn, str) and raw_turn:
            try:
                turn_id = uuid.UUID(raw_turn)
            except ValueError as exc:
                raise _Reject(INVALID_ENVELOPE) from exc
        if isinstance(raw_upload, str) and raw_upload:
            try:
                upload_id = uuid.UUID(raw_upload)
            except ValueError as exc:
                raise _Reject(INVALID_ENVELOPE) from exc
        elif isinstance(payload.get("artifact_id"), str):
            raise _Reject(INVALID_ENVELOPE)
        else:
            raise _Reject(INVALID_ENVELOPE)
        try:
            await complete_artifact_upload(
                db,
                settings,
                tenant_id,
                session_id,
                upload_id=upload_id,
                size=int(raw_size) if isinstance(raw_size, int) else None,
                sha256=sha256 if isinstance(sha256, str) else None,
                path=raw_path if isinstance(raw_path, str) else None,
                name=name if isinstance(name, str) else None,
                turn_id=turn_id,
                key_id=row.key_id,
                objects=objects if objects is not None else _object_store2(settings),
            )
        except Exception as exc:
            code, _message = _artifact_error(exc)
            raise _Reject(code) from exc
        return
    if envelope.type == "error":
        wakes.append(
            (
                session_id,
                await _store_event(
                    db,
                    tenant_id,
                    session_id,
                    type="agent.session.error",
                    data={
                        "message": str(payload.get("message") or ""),
                        "code": str(payload.get("code") or "internal"),
                    },
                ),
            )
        )
        return
    raise _Reject(NOT_IMPLEMENTED)


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Duplicate(Exception):
    """The ledger claim conflicted: this envelope already applied."""


def classify_incoming(data: Any) -> tuple[str, WorkerEnvelope | None, int]:
    """Sort one incoming socket message.

    Returns `(kind, envelope, raw_size)` with kind `envelope` (durable,
    ingest it), `ephemeral` (streaming deltas, never persisted), or
    `other` (not a v2 envelope; the legacy path handles it).
    """
    if not isinstance(data, dict) or data.get("v") != 2:
        return "other", None, 0
    import json

    raw_size = len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
    try:
        envelope = parse_envelope(data)
    except (ValidationError, UnknownMessageType, ValueError) as exc:
        raise _Reject(INVALID_ENVELOPE) from exc
    if envelope.type in EPHEMERAL_MESSAGE_TYPES:
        return "ephemeral", envelope, raw_size
    return "envelope", envelope, raw_size


def _artifact_error(exc: BaseException) -> tuple[str, str]:
    from apipi.config import DiskLimitError
    from apipi.store.blobs import ObjectStoreError

    if isinstance(exc, _Reject):
        return exc.reason, str(exc)
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code, str(exc)
    if isinstance(exc, (DiskLimitError, ObjectStoreError)):
        return "artifact_store", str(exc)
    return "ingest_error", str(exc)


def _blobs_for(settings: Any, objects: Any | None) -> Any:
    if objects is not None:
        if hasattr(objects, "delete_session"):
            return objects
        from apipi.store.blobs import ArtifactAdapter

        return ArtifactAdapter(objects)
    from apipi.store.blobs import blob_store as _blob_store

    try:
        return _blob_store(settings)
    except Exception:
        from apipi.store.blobs import MemoryBlobs

        return MemoryBlobs()


async def flush_batch(
    store: Store,
    queued: list[QueuedEnvelope],
    *,
    worker_id: uuid.UUID,
    settings: Any,
    metrics: Any,
    objects: Any | None = None,
) -> IngestOutcome:
    """Apply one batch in a single transaction; returns acks and wakes."""
    outcome = IngestOutcome()
    if not queued:
        return outcome
    turn_cache = _TurnCache()
    async with store.session() as db:
        rows: dict[uuid.UUID, SessionRow | None] = {}
        for queued_item in queued:
            envelope = queued_item.envelope
            session_id = envelope.session_id
            if session_id not in rows:
                rows[session_id] = await db.scalar(
                    select(SessionRow)
                    .where(SessionRow.id == session_id)
                    .with_for_update()
                )
            row = rows[session_id]
            tenant_id = row.tenant_id if row is not None else None
            wakes_before = len(outcome.wakes)
            try:
                if row is None or row.worker_id != worker_id or row.lease_id is None:
                    raise _Reject(NOT_LEASED)
                if queued_item.raw_size > MAX_MESSAGE_BYTES:
                    raise _Reject(OVERSIZE)
                if envelope.type in DEFERRED_TYPES:
                    # No sender emits these until #448/#449 own them; until
                    # then they are rejected like any other violation (and
                    # acked past, so the worker drops them).
                    raise _Reject(NOT_IMPLEMENTED)
                # Claim, validation, and apply share one savepoint so a
                # failed envelope rolls back completely: no partial
                # writes and no ledger row survive it. The ledger claim
                # still comes before validation so a replay of an applied
                # envelope skips quietly instead of failing validation
                # against newer state.
                try:
                    async with db.begin_nested():
                        await _claim(db, session_id, envelope.seq, envelope.type)
                        assert row is not None
                        reason = await _validate(
                            db,
                            envelope,
                            queued_item.raw_size,
                            worker_id=worker_id,
                            row=row,
                            turn_cache=turn_cache,
                        )
                        if reason is not None:
                            raise _Reject(reason)
                        await _apply(
                            db,
                            None,
                            envelope,
                            row,
                            settings=settings,
                            metrics=metrics,
                            wakes=outcome.wakes,
                            turn_cache=turn_cache,
                            presign_replies=outcome.presign_replies,
                            objects=objects,
                        )
                except _Duplicate:
                    pass
            except _Reject as rejected:
                del outcome.wakes[wakes_before:]
                reason = rejected.reason
                outcome.rejected.append((session_id, envelope.seq, reason))
                _count_reject(metrics, worker_id, session_id, tenant_id, reason)
            except Exception:
                del outcome.wakes[wakes_before:]
                log.exception(
                    "worker ingest apply failed",
                    extra={
                        "event": "worker.event.rejected",
                        "error_code": "ingest_error",
                        "session_id": str(session_id),
                        "worker_id": str(worker_id),
                        "seq": envelope.seq,
                    },
                )
                outcome.rejected.append((session_id, envelope.seq, "ingest_error"))
                _count_reject(metrics, worker_id, session_id, tenant_id, "ingest_error")
            outcome.acks[session_id] = max(
                outcome.acks.get(session_id, 0), envelope.seq
            )
        for session_id, last_seq in outcome.acks.items():
            row = rows.get(session_id)
            if row is not None and last_seq > row.worker_seq:
                row.worker_seq = last_seq
        await db.flush()
    return outcome


def _count_reject(
    metrics: Any,
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    reason: str,
) -> None:
    log_event(
        log,
        logging.WARNING,
        "worker envelope rejected",
        event="worker.event.rejected",
        error_code=reason,
        tenant_id=tenant_id,
        session_id=session_id,
        worker_id=worker_id,
    )
    if metrics is not None:
        metrics.observe_worker_protocol("envelope_rejected")


async def last_seq_for(
    store: Store, worker_id: uuid.UUID, session_id: uuid.UUID
) -> int | None:
    """Persisted ack cursor for one session, or None when not leased here."""
    async with store.session() as db:
        row = await db.scalar(select(SessionRow).where(SessionRow.id == session_id))
    if row is None or row.worker_id != worker_id or row.lease_id is None:
        return None
    return row.worker_seq
