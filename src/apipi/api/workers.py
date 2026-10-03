import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from apipi.common.event_bus import EventBus
from apipi.common.logutil import log_context, log_event
from apipi.protocol import (
    INVALID_REGISTER_REASON,
    PROTOCOL_VERSION,
    REGISTER_REQUIRED_REASON,
    REVOKED_REASON,
    SHARED_STORE_REASON,
    TOKEN_BOUND_REASON,
    UNAUTHORIZED_REASON,
    UNSUPPORTED_PROTOCOL_REASON,
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
    UnsupportedProtocol,
    WorkerEnvelope,
    WorkerEventMessage,
    parse_register,
    parse_worker_message,
    wire_type,
)
from apipi.services.ingest import (
    IngestBatcher,
    _Reject,
    classify_incoming,
    flush_batch,
)
from apipi.services.worker_tokens import (
    WORKER_TOKEN_PREFIX,
    authenticate_token,
    is_revoked_secret,
    token_revoked,
)
from apipi.store.engine import Store
from apipi.store.repo import clear_worker_api_instance, get_session_by_lease
from apipi.workerhub.heartbeat import heartbeat_worker
from apipi.workerhub.hub import WorkerHub
from apipi.workerhub.register import TokenBindingError, register_worker
from apipi.workerhub.wire import send_frame

log = logging.getLogger("apipi.worker")

SEARCH_MAX_INFLIGHT = 32
SLOW_HANDLER_SECONDS = 1.0

router = APIRouter()


def _bearer(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization")
    if header is None:
        return None
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    return token or None


async def _reject(websocket: WebSocket, error: str, *, reason: str) -> None:
    await send_frame(
        websocket,
        RejectMessage(error=error).to_wire(),
        metrics=getattr(websocket.app.state, "metrics", None),
    )
    await websocket.close(code=WORKER_CLOSE_CODE, reason=reason)


def _classify(message: dict[str, Any]) -> tuple[str, WorkerEnvelope | None, int]:
    try:
        return classify_incoming(message)
    except _Reject:
        return "garbage", None, 0


def _log_garbage(
    hub: WorkerHub, conn: Any, metrics: Any, message: dict[str, Any]
) -> None:
    hub.observe_protocol("envelope_rejected")
    raw_session = message.get("session_id")
    try:
        session_id = uuid.UUID(str(raw_session)) if raw_session else None
    except ValueError:
        session_id = None
    conn.warnings.warning(
        "worker envelope rejected",
        event="worker.event.rejected",
        error_code="invalid_envelope",
        session_id=session_id,
    )
    if metrics is not None:
        metrics.observe_worker_protocol("envelope_rejected")
        metrics.observe_worker_ingest("unknown", "rejected")
        metrics.observe_worker_ingest_rejected("invalid_envelope")


async def _answer_search(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    service: Any,
    conn: Any,
    message: dict[str, Any],
) -> None:
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
        async with send_lock:
            await send_frame(
                websocket, reply, metrics=getattr(websocket.app.state, "metrics", None)
            )
    except Exception:
        log.warning(
            "search reply not sent",
            extra={"event": "search.reply.failed", "error_code": "socket_closed"},
        )


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


async def _flush_envelopes(
    store: Store,
    event_hub: EventBus,
    conn: Any,
    batcher: IngestBatcher,
    settings: Any,
    metrics: Any,
    websocket: WebSocket,
    objects: Any | None = None,
    send_lock: asyncio.Lock | None = None,
) -> None:
    queued = batcher.take()
    if not queued:
        return
    lifecycle = websocket.app.state.lifecycle
    flush_started = time.monotonic()
    outcome = await flush_batch(
        store,
        queued,
        worker_id=conn.worker_id,
        settings=settings,
        metrics=metrics,
        objects=objects,
        run_mode=conn.run_mode,
    )
    if metrics is not None:
        metrics.observe_worker_ingest_batch(
            seconds=time.monotonic() - flush_started, size=len(queued)
        )
    lock = send_lock if send_lock is not None else asyncio.Lock()
    await websocket.app.state.workers.renew_on_activity(store, conn)
    for session_id, last_seq in sorted(
        outcome.acks.items(), key=lambda item: str(item[0])
    ):
        async with lock:
            await send_frame(
                websocket,
                CumulativeAck(session_id=session_id, last_seq=last_seq).to_wire(),
                metrics=metrics,
            )
    for reply in outcome.presign_replies:
        async with lock:
            await send_frame(websocket, reply, metrics=metrics)
    for session_id, body in outcome.wakes:
        await event_hub.publish(session_id, body)
    if outcome.lifecycle and lifecycle is not None:
        from apipi.services.ingest import emit_lifecycle_intents

        for intent in outcome.lifecycle:
            if intent.fields.get("run_mode") is None:
                intent.fields["run_mode"] = conn.run_mode
        emit_lifecycle_intents(lifecycle, outcome.lifecycle)
    if outcome.wipes:
        from apipi.services.worker_artifacts import wipe_artifact_store

        blobs = websocket.app.state.blobs
        for tenant_id, key_id, session_id in outcome.wipes:
            try:
                await wipe_artifact_store(blobs, tenant_id, key_id, session_id)
            except Exception:
                log.warning(
                    "worker wipe blob delete failed",
                    extra={
                        "event": "worker.wipe.failed",
                        "error_code": "artifact_store",
                        "session_id": str(session_id),
                    },
                )


@router.websocket("/internal/worker")
async def worker_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: WorkerHub = websocket.app.state.workers
    store: Store = websocket.app.state.store
    event_hub: EventBus = websocket.app.state.event_hub
    metrics = websocket.app.state.metrics
    raw_token = _bearer(websocket)
    if raw_token is None or not raw_token.startswith(WORKER_TOKEN_PREFIX):
        hub.observe_connect("unauthorized")
        await _reject(websocket, "unauthorized", reason=UNAUTHORIZED_REASON)
        return
    token = await authenticate_token(store, raw_token)
    if token is None:
        if await is_revoked_secret(store, raw_token):
            hub.observe_connect("revoked")
            log.warning("worker token revoked", extra={"event": "worker.auth.revoked"})
            await _reject(websocket, "revoked", reason=REVOKED_REASON)
        else:
            hub.observe_connect("unauthorized")
            await _reject(websocket, "unauthorized", reason=UNAUTHORIZED_REASON)
        return
    try:
        raw: Any = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except TimeoutError:
        hub.observe_connect("register_timeout")
        await websocket.close(code=WORKER_CLOSE_CODE)
        return
    except WebSocketDisconnect:
        hub.observe_connect("closed")
        return
    if metrics is not None:
        metrics.observe_worker_message("in", wire_type(raw))
    if not isinstance(raw, dict) or raw.get("type") != "register":
        hub.observe_connect("invalid_register")
        await _reject(websocket, "register required", reason=REGISTER_REQUIRED_REASON)
        return
    try:
        register = parse_register(raw)
    except UnsupportedProtocol:
        hub.observe_connect("unsupported_protocol")
        log.warning(
            "worker protocol rejected",
            extra={
                "event": "worker.protocol.rejected",
                "reason": "unsupported_protocol",
            },
        )
        await _reject(
            websocket, "unsupported_protocol", reason=UNSUPPORTED_PROTOCOL_REASON
        )
        return
    except ValidationError:
        hub.observe_connect("invalid_register")
        await _reject(websocket, "invalid register", reason=INVALID_REGISTER_REASON)
        return
    try:
        conn = await register_worker(hub, store, websocket, register, token, event_hub)
    except TokenBindingError:
        hub.observe_connect("token_bound")
        log.warning(
            "worker token bound to another worker",
            extra={"event": "worker.auth.bound"},
        )
        await _reject(websocket, "token_bound", reason=TOKEN_BOUND_REASON)
        return
    if conn is None:
        hub.observe_connect("invalid_register")
        await _reject(websocket, "invalid register", reason=INVALID_REGISTER_REASON)
        return
    hub.observe_connect("ok")
    with log_context(worker_id=str(conn.worker_id), connection_id=conn.connection_id):
        await _serve_connection(websocket, hub, store, event_hub, conn)


async def _serve_connection(
    websocket: WebSocket,
    hub: WorkerHub,
    store: Store,
    event_hub: EventBus,
    conn: Any,
) -> None:
    settings = websocket.app.state.settings
    metrics = websocket.app.state.metrics
    opened = time.monotonic()
    reason = "error"
    info = (str(conn.worker_id), str(PROTOCOL_VERSION), conn.version, conn.run_mode)
    if metrics is not None:
        metrics.set_worker_info(
            worker_id=info[0], protocol=info[1], version=info[2], run_mode=info[3]
        )
    log_event(
        log,
        logging.INFO,
        "worker connected",
        event="worker.connected",
        run_mode=conn.run_mode,
        capacity=conn.capacity,
        version=conn.version,
        generation=conn.generation,
        leases=len(conn.leases),
    )
    batcher = IngestBatcher(max_messages=settings.worker_ingest_batch_size)
    window = settings.worker_ingest_batch_window.total_seconds()
    send_lock = asyncio.Lock()
    search_tasks: set[asyncio.Task[None]] = set()
    handling: tuple[str, float] | None = None

    def finish_handling() -> None:
        nonlocal handling
        if handling is None:
            return
        kind_label, since = handling
        handling = None
        elapsed = time.monotonic() - since
        if metrics is not None:
            metrics.observe_worker_handle(kind_label, elapsed)
        if elapsed > SLOW_HANDLER_SECONDS:
            conn.warnings.warning(
                "worker message handler slow",
                event="worker.handler.slow",
                error_code="handler_slow",
                type=kind_label,
                seconds=round(elapsed, 3),
            )

    try:
        while True:
            finish_handling()
            timeout = batcher.poll_timeout(window)
            try:
                if timeout is None:
                    message = await websocket.receive_json()
                else:
                    message = await asyncio.wait_for(
                        websocket.receive_json(), timeout=timeout
                    )
            except TimeoutError:
                message = None
            if message is not None:
                type_label = wire_type(message)
                handling = (type_label, time.monotonic())
                if metrics is not None or log.isEnabledFor(logging.DEBUG):
                    size = len(json.dumps(message, separators=(",", ":")))
                    if metrics is not None:
                        metrics.observe_worker_message("in", type_label, size)
                    log.debug(
                        "worker message in",
                        extra={
                            "event": "worker.message",
                            "type": type_label,
                            "size": size,
                        },
                    )
            if message is None or batcher.should_flush(window):
                if len(batcher):
                    await _flush_envelopes(
                        store,
                        event_hub,
                        conn,
                        batcher,
                        settings,
                        metrics,
                        websocket,
                        websocket.app.state.objects,
                        send_lock,
                    )
                if message is None:
                    continue
            if not isinstance(message, dict):
                continue
            kind, envelope, raw_size = _classify(message)
            if kind == "envelope" and envelope is not None:
                batcher.add(envelope, raw_size)
                if batcher.should_flush(window):
                    await _flush_envelopes(
                        store,
                        event_hub,
                        conn,
                        batcher,
                        settings,
                        metrics,
                        websocket,
                        websocket.app.state.objects,
                        send_lock,
                    )
                continue
            if kind == "ephemeral" and envelope is not None:
                await hub.handle_delta(store, event_hub, conn, envelope)
                continue
            if kind == "garbage":
                _log_garbage(hub, conn, metrics, message)
                continue
            msg_type = message.get("type")
            if msg_type not in WORKER_IN:
                continue
            if msg_type == "search.request":
                search = getattr(websocket.app.state, "search", None)
                if len(search_tasks) >= SEARCH_MAX_INFLIGHT or search is None:
                    busy = _search_failure(
                        message, "search_unavailable", "search is busy"
                    )
                    if busy is not None:
                        async with send_lock:
                            await send_frame(websocket, busy, metrics=metrics)
                    continue
                task = asyncio.create_task(
                    _answer_search(websocket, send_lock, search, conn, message)
                )
                search_tasks.add(task)
                task.add_done_callback(search_tasks.discard)
                continue
            try:
                parsed = parse_worker_message(message)
            except ValidationError:
                continue
            if isinstance(parsed, StoreProof):
                from apipi.workerhub.register import verify_store_proof

                if not verify_store_proof(
                    settings, parsed.marker, parsed.nonce, expected=conn.store_proof
                ):
                    hub.observe_protocol("invalid_register")
                    log.warning(
                        "worker shared store proof failed",
                        extra={
                            "event": "worker.store.rejected",
                            "worker_id": str(conn.worker_id),
                        },
                    )
                    reason = "protocol_violation"
                    await send_frame(
                        websocket,
                        RejectMessage(error=SHARED_STORE_REASON).to_wire(),
                        metrics=metrics,
                    )
                    await websocket.close(
                        code=WORKER_CLOSE_CODE, reason=SHARED_STORE_REASON
                    )
                    return
                conn.store_proof = None
                continue
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
                    store, event_hub, conn.worker_id, reported, unleased
                )
                reply = InventoryReply(
                    revoke=[RevokeEntry.model_validate(entry) for entry in revoke],
                    ttl={
                        uuid.UUID(key): TtlEntry.model_validate(value)
                        for key, value in ttl.items()
                    },
                )
                async with send_lock:
                    await send_frame(websocket, reply.to_wire(), metrics=metrics)
                continue
            if isinstance(parsed, SandboxSeenMessage):
                owned = await hub.owned_sessions(store, conn, parsed.session_ids)
                if owned:
                    from apipi.services.sandbox_status import touch_seen

                    await touch_seen(store, owned)
                continue
            if isinstance(parsed, HeartbeatMessage):
                if conn.token_id is not None and await token_revoked(
                    store, conn.token_id
                ):
                    reason = "revoked"
                    log.warning(
                        "worker token revoked",
                        extra={
                            "event": "worker.auth.revoked",
                            "worker_id": str(conn.worker_id),
                        },
                    )
                    await websocket.close(code=WORKER_CLOSE_CODE, reason=REVOKED_REASON)
                    return
                await heartbeat_worker(hub, store, conn, parsed)
                continue
            if isinstance(parsed, LeaseAck):
                if parsed.lease_id not in conn.leases:
                    continue
                await hub.ack(parsed.lease_id, str(parsed.id))
                await hub.renew_on_activity(store, conn)
                continue
            if isinstance(parsed, LeaseRelease):
                if parsed.lease_id not in conn.leases:
                    continue
                if len(batcher):
                    await _flush_envelopes(
                        store,
                        event_hub,
                        conn,
                        batcher,
                        settings,
                        metrics,
                        websocket,
                        websocket.app.state.objects,
                        send_lock,
                    )
                async with store.session() as db:
                    row = await get_session_by_lease(db, parsed.lease_id)
                if row is None:
                    continue
                await hub.release(
                    store, row.tenant_id, parsed.session_id, parsed.lease_id
                )
                continue
            if isinstance(parsed, WorkerEventMessage):
                if parsed.lease_id not in conn.leases:
                    continue
                await hub.handle_event(
                    store,
                    event_hub,
                    lease_id=parsed.lease_id,
                    event_type=parsed.event_type,
                    data=parsed.data,
                )
    except WebSocketDisconnect as exc:
        reason = "clean" if exc.code in (1000, 1001) else "error"
    finally:
        finish_handling()
        pending = list(search_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        reason = conn.disconnect_reason or reason
        unacked = sum(1 for lease_id in conn.leases if lease_id in hub._unacked)
        leases = len(conn.leases)
        await hub.detach(conn.worker_id, conn)
        hub.observe_disconnect(reason)
        if metrics is not None:
            metrics.clear_worker_info(
                worker_id=info[0], protocol=info[1], version=info[2], run_mode=info[3]
            )
        log_event(
            log,
            logging.INFO,
            "worker disconnected",
            event="worker.disconnected",
            reason=reason,
            duration_s=round(time.monotonic() - opened, 3),
            leases=leases,
            unacked_commands=unacked,
        )
        async with store.session() as db:
            await clear_worker_api_instance(
                db, conn.worker_id, instance_id=hub.settings.instance_id
            )


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
