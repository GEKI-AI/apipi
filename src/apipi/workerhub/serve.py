import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from apipi.common.event_bus import EventBus
from apipi.common.logutil import log_event
from apipi.common.wirewatch import note_unknown_fields, note_unknown_type
from apipi.protocol import (
    REVOKED_REASON,
    SHARED_STORE_REASON,
    WORKER_CLOSE_CODE,
    WORKER_IN,
    CumulativeAck,
    HeartbeatMessage,
    InventoryMessage,
    InventoryReply,
    LeaseAck,
    LeaseRelease,
    RejectMessage,
    RevokeEntry,
    SandboxSeenMessage,
    SearchReply,
    StoreProof,
    TtlEntry,
    WorkerEnvelope,
    WorkerEventMessage,
    collect_unknown_fields,
    parse_worker_message,
    wire_bytes,
    wire_type,
)
from apipi.services.ingest import (
    UNKNOWN_TYPE,
    IngestBatcher,
    IngestOutcome,
    QueuedEnvelope,
    _Reject,
    classify_incoming,
    emit_lifecycle_intents,
    flush_batch,
)
from apipi.services.worker_tokens import token_revoked
from apipi.store.engine import Store
from apipi.store.repo import get_session_by_lease
from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.heartbeat import heartbeat_worker, parse_heartbeat
from apipi.workerhub.hub import WorkerHub
from apipi.workerhub.register import verify_store_proof

log = logging.getLogger("apipi.worker")

SEARCH_MAX_INFLIGHT = 32
SLOW_HANDLER_SECONDS = 1.0
CONTROL_QUEUE_LIMIT = 256
INGEST_QUEUE_LIMIT = 2048
DELTA_QUEUE_LIMIT = 1024
RELEASE_ATTEMPTS = 3
INGEST_ATTEMPTS = 3
RETRY_DELAYS = (0.1, 0.5, 1.0)
CLOSE_TIMEOUT = 5.0
WS_MAX_SIZE = 4 * 1024 * 1024
KEEPALIVE_CLOSE_CODE = 1011


@dataclass
class _Control:
    label: str
    parsed: BaseModel


@dataclass
class _Envelope:
    envelope: WorkerEnvelope
    raw_size: int


def _classify(
    message: dict[str, Any], size: int | None = None
) -> tuple[str, WorkerEnvelope | None, int]:
    try:
        return classify_incoming(message, size)
    except _Reject as rejected:
        if rejected.reason == UNKNOWN_TYPE:
            return "unknown_type", None, 0
        return "garbage", None, 0


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _search_failure(
    message: dict[str, Any], code: str, text: str
) -> dict[str, Any] | None:
    session_id = _uuid(message.get("session_id"))
    request_id = _uuid(message.get("request_id"))
    if session_id is None or request_id is None:
        return None
    return SearchReply(
        session_id=session_id,
        request_id=request_id,
        ok=False,
        results=[],
        code=code,
        message=text,
    ).to_wire()


def disconnect_reason(exc: WebSocketDisconnect) -> str:
    if exc.code in (1000, 1001):
        return "clean"
    if exc.code == KEEPALIVE_CLOSE_CODE:
        return "ping_timeout"
    return "error"


class ConnectionServer:
    """Serves one registered worker socket until it closes.

    The receive loop only reads frames, parses them, and hands each to a
    lane. Control messages (`heartbeat`, `lease.ack`, `lease.release`,
    `inventory`, `sandbox.seen`, `store.proof`) have their own lane, so
    slow ingest, presign, or artifact work never delays them. Durable
    envelopes and legacy events share one ingest lane that keeps their
    order. Live deltas have a third lane that drops when it is full.
    Every write goes through the connection's writer. A failure while
    handling one message is logged and counted and the socket stays
    open. The socket closes on a protocol violation, a revoked token, a
    superseded connection, a write timeout, an ingest transaction that
    still fails after retries, or when the peer goes away.
    """

    def __init__(
        self,
        websocket: WebSocket,
        hub: WorkerHub,
        store: Store,
        event_hub: EventBus,
        conn: WorkerConnection,
    ) -> None:
        self.websocket = websocket
        self.hub = hub
        self.store = store
        self.event_hub = event_hub
        self.conn = conn
        self.settings = websocket.app.state.settings
        self.metrics = websocket.app.state.metrics
        self._control: asyncio.Queue[_Control] = asyncio.Queue(CONTROL_QUEUE_LIMIT)
        self._ingest: asyncio.Queue[_Envelope | _Control] = asyncio.Queue(
            INGEST_QUEUE_LIMIT
        )
        self._deltas: asyncio.Queue[WorkerEnvelope] = asyncio.Queue(DELTA_QUEUE_LIMIT)
        self._unflushed: dict[uuid.UUID, int] = {}
        self._search_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> str:
        conn = self.conn
        lanes = [
            asyncio.create_task(self._read(), name="worker_reader"),
            asyncio.create_task(self._run_control(), name="worker_control"),
            asyncio.create_task(self._run_ingest(), name="worker_ingest"),
            asyncio.create_task(self._run_deltas(), name="worker_deltas"),
        ]
        watcher = asyncio.create_task(conn.closing.wait(), name="worker_closing")
        waiting: set[asyncio.Task[Any]] = {*lanes, watcher}
        if conn.writer.task is not None:
            waiting.add(conn.writer.task)
        reason = "error"
        try:
            done, _pending = await asyncio.wait(
                waiting, return_when=asyncio.FIRST_COMPLETED
            )
            if (
                conn.writer.task in done
                and lanes[0] not in done
                and watcher not in done
            ):
                await asyncio.wait({lanes[0]}, timeout=1.0)
                done = {*done, *(task for task in lanes[:1] if task.done())}
            reason = self._reason(done, lanes[0], watcher)
        finally:
            everything = [*lanes, watcher, *self._search_tasks]
            for task in everything:
                task.cancel()
            await asyncio.gather(*everything, return_exceptions=True)
            await conn.writer.stop()
            await self._close_socket()
        return conn.disconnect_reason or reason

    def _reason(
        self,
        done: set[asyncio.Task[Any]],
        reader: asyncio.Task[Any],
        watcher: asyncio.Task[Any],
    ) -> str:
        if watcher in done:
            return self.conn.disconnect_reason or "error"
        if reader in done and not reader.cancelled():
            exc = reader.exception()
            if isinstance(exc, WebSocketDisconnect):
                return disconnect_reason(exc)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                log_event(
                    log,
                    logging.ERROR,
                    "worker connection task failed",
                    event="worker.connection.failed",
                    error_code="connection_task_failed",
                    exc_info=error,
                    task=task.get_name(),
                    error=type(error).__name__,
                )
        return "error"

    async def _close_socket(self) -> None:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return
        conn = self.conn
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.websocket.close(code=conn.close_code, reason=conn.close_text),
                timeout=CLOSE_TIMEOUT,
            )

    async def _read(self) -> None:
        while True:
            frame = await self.websocket.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code", 1000), frame.get("reason"))
            text = frame.get("text")
            if text is None:
                self._invalid_frame("binary")
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self._invalid_frame("invalid_json")
                continue
            if not isinstance(message, dict):
                self._invalid_frame("not_an_object")
                continue
            await self._dispatch(message, wire_bytes(text))

    def _invalid_frame(self, why: str) -> None:
        self.hub.observe_protocol("frame_invalid")
        self.conn.warnings.warning(
            "worker frame skipped",
            event="worker.frame.invalid",
            error_code="frame_invalid",
            worker_id=self.conn.worker_id,
            why=why,
        )

    async def _dispatch(self, message: dict[str, Any], size: int) -> None:
        conn = self.conn
        type_label = wire_type(message)
        if self.metrics is not None:
            self.metrics.observe_worker_message("in", type_label, size)
        log.debug(
            "worker message in",
            extra={"event": "worker.message", "type": type_label, "size": size},
        )
        with collect_unknown_fields() as unknown:
            kind, envelope, raw_size = _classify(message, size)
            parsed, invalid = self._parse_control(message, kind, type_label)
        if unknown:
            note_unknown_fields(unknown, metrics=self.metrics, side="api")
        if kind == "unknown_type":
            note_unknown_type(
                "type", message.get("type"), metrics=self.metrics, side="api"
            )
            return
        if kind == "envelope" and envelope is not None:
            self._unflushed[envelope.session_id] = (
                self._unflushed.get(envelope.session_id, 0) + 1
            )
            await self._put(self._ingest, _Envelope(envelope, raw_size))
            return
        if kind == "ephemeral" and envelope is not None:
            try:
                self._deltas.put_nowait(envelope)
            except asyncio.QueueFull:
                self.hub.observe_protocol("delta.queue_full")
                conn.warnings.warning(
                    "worker delta dropped; the delta lane is full",
                    event="worker.delta.queue_full",
                    error_code="delta_queue_full",
                    worker_id=conn.worker_id,
                )
            return
        if kind == "garbage":
            self._log_garbage(message)
            return
        if invalid:
            self.hub.observe_protocol("message_invalid")
            conn.warnings.warning(
                "worker message invalid; skipped",
                event="worker.message.skipped",
                error_code="message_invalid",
                key=f"worker.message.invalid:{type_label}",
                worker_id=conn.worker_id,
                type=type_label,
            )
            return
        msg_type = message.get("type")
        if msg_type not in WORKER_IN:
            note_unknown_type("type", msg_type, metrics=self.metrics, side="api")
            return
        if msg_type == "search.request":
            await self._start_search(message)
            return
        ordered = isinstance(parsed, WorkerEventMessage) or (
            isinstance(parsed, LeaseRelease)
            and bool(self._unflushed.get(parsed.session_id))
        )
        if ordered:
            await self._put(self._ingest, _Control(type_label, parsed))
        elif isinstance(
            parsed,
            StoreProof
            | InventoryMessage
            | SandboxSeenMessage
            | HeartbeatMessage
            | LeaseAck
            | LeaseRelease,
        ):
            await self._put(self._control, _Control(type_label, parsed))

    def _parse_control(
        self, message: dict[str, Any], kind: str, type_label: str
    ) -> tuple[BaseModel | None, bool]:
        msg_type = message.get("type")
        if kind != "other" or msg_type not in WORKER_IN or msg_type == "search.request":
            return None, False
        try:
            if msg_type == "heartbeat":
                return parse_heartbeat(self.hub, self.conn, message), False
            return parse_worker_message(message), False
        except ValidationError:
            return None, True

    async def _put(self, queue: "asyncio.Queue[Any]", item: Any) -> None:
        if queue.full():
            self.hub.observe_protocol("lane.backpressure")
            self.conn.warnings.warning(
                "worker lane is full; the socket waits for it",
                event="worker.lane.backpressure",
                error_code="lane_backpressure",
                worker_id=self.conn.worker_id,
            )
        await queue.put(item)

    def _log_garbage(self, message: dict[str, Any]) -> None:
        self.hub.observe_protocol("envelope_rejected")
        session_id = _uuid(message.get("session_id"))
        self.conn.warnings.warning(
            "worker envelope rejected",
            event="worker.event.rejected",
            error_code="invalid_envelope",
            session_id=session_id,
        )
        if self.metrics is not None:
            self.metrics.observe_worker_protocol("envelope_rejected")
            self.metrics.observe_worker_ingest("unknown", "rejected")
            self.metrics.observe_worker_ingest_rejected("invalid_envelope")

    def _finish(self, label: str, started: float) -> None:
        elapsed = time.monotonic() - started
        if self.metrics is not None:
            self.metrics.observe_worker_handle(label, elapsed)
        if elapsed > SLOW_HANDLER_SECONDS:
            self.conn.warnings.warning(
                "worker message handler slow",
                event="worker.handler.slow",
                error_code="handler_slow",
                key=f"worker.handler.slow:{label}",
                type=label,
                seconds=round(elapsed, 3),
            )

    def _failed(self, label: str, exc: BaseException) -> None:
        self.hub.observe_protocol("message_failed")
        self.conn.warnings.warning(
            "worker message failed; the connection stays open",
            event="worker.message.failed",
            error_code="message_failed",
            key=f"worker.message.failed:{label}",
            exc_info=exc,
            worker_id=self.conn.worker_id,
            type=label,
            error=type(exc).__name__,
            transient=isinstance(exc, DBAPIError | OSError | TimeoutError),
        )

    async def _guarded(
        self,
        label: str,
        handler: Callable[[], Awaitable[object]],
        *,
        attempts: int = 1,
    ) -> None:
        started = time.monotonic()
        try:
            for attempt in range(attempts):
                try:
                    await handler()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._failed(label, exc)
                    if attempt + 1 >= attempts:
                        return
                    self.hub.observe_protocol("message_retried")
                    await asyncio.sleep(RETRY_DELAYS[min(attempt, 2)])
        finally:
            self._finish(label, started)

    async def _run_control(self) -> None:
        while True:
            item = await self._control.get()
            await self._guarded(
                item.label,
                lambda item=item: self._handle(item.parsed),
                attempts=RELEASE_ATTEMPTS
                if isinstance(item.parsed, LeaseRelease)
                else 1,
            )

    async def _handle(self, parsed: BaseModel) -> None:
        conn = self.conn
        hub = self.hub
        store = self.store
        if isinstance(parsed, StoreProof):
            ok = await asyncio.to_thread(
                verify_store_proof,
                self.settings,
                parsed.marker,
                parsed.nonce,
                expected=conn.store_proof,
            )
            if not ok:
                hub.observe_protocol("invalid_register")
                log.warning(
                    "worker shared store proof failed",
                    extra={
                        "event": "worker.store.rejected",
                        "worker_id": str(conn.worker_id),
                    },
                )
                with contextlib.suppress(Exception):
                    await conn.send(RejectMessage(error=SHARED_STORE_REASON).to_wire())
                conn.request_close(
                    "protocol_violation",
                    code=WORKER_CLOSE_CODE,
                    text=SHARED_STORE_REASON,
                )
                return
            conn.store_proof = None
            return
        if isinstance(parsed, HeartbeatMessage):
            if conn.token_id is not None and await token_revoked(store, conn.token_id):
                log.warning(
                    "worker token revoked",
                    extra={
                        "event": "worker.auth.revoked",
                        "worker_id": str(conn.worker_id),
                    },
                )
                conn.request_close(
                    "revoked", code=WORKER_CLOSE_CODE, text=REVOKED_REASON
                )
                return
            if not await heartbeat_worker(hub, store, conn, parsed):
                hub.observe_protocol("superseded")
                log_event(
                    log,
                    logging.WARNING,
                    "worker connection superseded; closing it",
                    event="worker.connection.superseded",
                    error_code="superseded",
                    generation=conn.generation,
                )
                conn.request_close("takeover")
            return
        if isinstance(parsed, LeaseAck):
            if parsed.lease_id not in conn.leases:
                return
            await hub.ack(parsed.lease_id, str(parsed.id))
            await hub.renew_on_activity(store, conn)
            return
        if isinstance(parsed, LeaseRelease):
            if parsed.lease_id not in conn.leases:
                return
            async with store.session() as db:
                row = await get_session_by_lease(db, parsed.lease_id)
            if row is None:
                return
            await hub.release(store, row.tenant_id, parsed.session_id, parsed.lease_id)
            return
        if isinstance(parsed, InventoryMessage):
            reported: dict[uuid.UUID, uuid.UUID] = {}
            unleased: list[uuid.UUID] = []
            for entry in parsed.sessions:
                if entry.lease_id is None:
                    if entry.session_id not in unleased:
                        unleased.append(entry.session_id)
                else:
                    reported[entry.session_id] = entry.lease_id
            revoke, ttl = await hub.reconcile_inventory(
                store, self.event_hub, conn.worker_id, reported, unleased, conn=conn
            )
            reply = InventoryReply(
                revoke=[RevokeEntry.model_validate(entry) for entry in revoke],
                ttl={
                    uuid.UUID(key): TtlEntry.model_validate(value)
                    for key, value in ttl.items()
                },
            )
            conn.writer.send_nowait(reply.to_wire())
            return
        if isinstance(parsed, SandboxSeenMessage):
            owned = await hub.owned_sessions(store, conn, parsed.session_ids)
            if owned:
                from apipi.services.sandbox_status import touch_seen

                await touch_seen(store, owned)

    async def _run_deltas(self) -> None:
        while True:
            envelope = await self._deltas.get()
            await self._guarded(
                envelope.type,
                lambda envelope=envelope: self.hub.handle_delta(
                    self.store, self.event_hub, self.conn, envelope
                ),
            )

    async def _run_ingest(self) -> None:
        batcher = IngestBatcher(max_messages=self.settings.worker_ingest_batch_size)
        window = self.settings.worker_ingest_batch_window.total_seconds()
        while True:
            timeout = batcher.poll_timeout(window)
            item: _Envelope | _Control | None = None
            try:
                if timeout is None:
                    item = await self._ingest.get()
                else:
                    item = await asyncio.wait_for(self._ingest.get(), timeout=timeout)
            except TimeoutError:
                item = None
            if isinstance(item, _Control):
                if isinstance(item.parsed, LeaseRelease):
                    await self._flush(batcher)
                await self._guarded(
                    item.label,
                    lambda item=item: self._handle_ordered(item.parsed),
                    attempts=RELEASE_ATTEMPTS
                    if isinstance(item.parsed, LeaseRelease)
                    else 1,
                )
                continue
            if item is not None:
                batcher.add(item.envelope, item.raw_size)
            if len(batcher) and (item is None or batcher.should_flush(window)):
                await self._flush(batcher)

    async def _handle_ordered(self, parsed: BaseModel) -> None:
        if isinstance(parsed, LeaseRelease):
            await self._handle(parsed)
            return
        if isinstance(parsed, WorkerEventMessage):
            if parsed.lease_id not in self.conn.leases:
                return
            await self.hub.handle_event(
                self.store,
                self.event_hub,
                lease_id=parsed.lease_id,
                event_type=parsed.event_type,
                data=parsed.data,
            )

    async def _flush(self, batcher: IngestBatcher) -> None:
        queued = batcher.take()
        if not queued:
            return
        started = time.monotonic()
        try:
            await self._apply_batch(queued)
        finally:
            for item in queued:
                session_id = item.envelope.session_id
                left = self._unflushed.get(session_id, 0) - 1
                if left > 0:
                    self._unflushed[session_id] = left
                else:
                    self._unflushed.pop(session_id, None)
            for label in {item.envelope.type for item in queued}:
                self._finish(label, started)

    async def _apply_batch(self, queued: list[QueuedEnvelope]) -> None:
        """Apply a batch, then retry what a temporary failure left unapplied.

        An envelope that failed for a temporary reason is not acked, and
        neither is anything after it in its session. Those envelopes go
        through again, up to `INGEST_ATTEMPTS` tries in all. If they
        still fail, the socket closes and the worker replays them.
        """
        conn = self.conn
        pending = queued
        started = time.monotonic()
        for attempt in range(INGEST_ATTEMPTS):
            try:
                outcome = await flush_batch(
                    self.store,
                    pending,
                    worker_id=conn.worker_id,
                    settings=self.settings,
                    metrics=self.metrics,
                    objects=self.websocket.app.state.objects,
                    run_mode=conn.run_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed("ingest", exc)
            else:
                if self.metrics is not None:
                    self.metrics.observe_worker_ingest_batch(
                        seconds=time.monotonic() - started, size=len(pending)
                    )
                await self._after_flush(outcome)
                pending = outcome.retry
                if not pending:
                    return
            if attempt + 1 >= INGEST_ATTEMPTS:
                break
            self.hub.observe_protocol("ingest.retried")
            await asyncio.sleep(RETRY_DELAYS[min(attempt, 2)])
        self.hub.observe_protocol("ingest.failed")
        conn.request_close("ingest_failed", code=1011, text="ingest_failed")

    async def _after_flush(self, outcome: IngestOutcome) -> None:
        conn = self.conn
        for reply in outcome.presign_replies:
            conn.writer.send_nowait(reply)
        for session_id, last_seq in sorted(
            outcome.acks.items(), key=lambda item: str(item[0])
        ):
            conn.writer.send_nowait(
                CumulativeAck(session_id=session_id, last_seq=last_seq).to_wire()
            )
        await self._step("renew", self.hub.renew_on_activity(self.store, conn))
        for session_id, body in outcome.wakes:
            self.hub.note_stored_events(session_id, [body])
        for session_id, body in outcome.wakes:
            await self._step("publish", self.event_hub.publish(session_id, body))
        lifecycle = self.websocket.app.state.lifecycle
        if outcome.lifecycle and lifecycle is not None:
            for intent in outcome.lifecycle:
                if intent.fields.get("run_mode") is None:
                    intent.fields["run_mode"] = conn.run_mode
            emit_lifecycle_intents(lifecycle, outcome.lifecycle)
        if outcome.wipes:
            from apipi.services.worker_artifacts import wipe_artifact_store

            blobs = self.websocket.app.state.blobs
            for tenant_id, key_id, session_id in outcome.wipes:
                await self._step(
                    "wipe", wipe_artifact_store(blobs, tenant_id, key_id, session_id)
                )
        for session_id in outcome.stopped:
            self.hub.note_stopped(session_id)

    async def _step(self, label: str, work: Awaitable[object]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(f"ingest.{label}", exc)

    async def _start_search(self, message: dict[str, Any]) -> None:
        search = getattr(self.websocket.app.state, "search", None)
        if len(self._search_tasks) >= SEARCH_MAX_INFLIGHT or search is None:
            busy = _search_failure(message, "search_unavailable", "search is busy")
            if busy is not None:
                self.conn.writer.send_nowait(busy)
            return
        task = asyncio.create_task(self._answer_search(search, message))
        self._search_tasks.add(task)
        task.add_done_callback(self._search_tasks.discard)

    async def _answer_search(self, service: Any, message: dict[str, Any]) -> None:
        conn = self.conn
        reply: dict[str, Any] | None = None
        try:
            reply = await service.handle_request(
                message, worker_id=conn.worker_id, leases=set(conn.leases)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "search request failed",
                extra={"event": "search.request", "error_code": "search_failed"},
            )
            reply = _search_failure(message, "search_failed", "search failed")
        if reply is None:
            return
        try:
            await conn.send(reply)
        except Exception:
            log.warning(
                "search reply not sent",
                extra={"event": "search.reply.failed", "error_code": "socket_closed"},
            )
