import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from apipi.services.event_bus import EventBus
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
from apipi.worker.hub import (
    WORKER_IN,
    TokenBindingError,
    WorkerHub,
    heartbeat_worker,
    register_worker,
)
from apipi.worker.protocol import (
    UNSUPPORTED_PROTOCOL_REASON,
    WORKER_CLOSE_CODE,
    CumulativeAck,
    UnsupportedProtocol,
    WorkerEnvelope,
    parse_register,
)

log = logging.getLogger("apipi.worker")

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
    await websocket.send_json({"ok": False, "error": error})
    await websocket.close(code=WORKER_CLOSE_CODE, reason=reason)


def _classify(message: dict[str, Any]) -> tuple[str, WorkerEnvelope | None, int]:
    try:
        return classify_incoming(message)
    except _Reject:
        return "garbage", None, 0


def _log_garbage(hub: WorkerHub, metrics: Any, message: dict[str, Any]) -> None:
    hub.observe_protocol("envelope_rejected")
    raw_session = message.get("session_id")
    try:
        session_id = uuid.UUID(str(raw_session)) if raw_session else None
    except ValueError:
        session_id = None
    log.warning(
        "worker envelope rejected",
        extra={
            "event": "worker.event.rejected",
            "error_code": "invalid_envelope",
            "session_id": str(session_id) if session_id is not None else None,
        },
    )
    if metrics is not None:
        metrics.observe_worker_protocol("envelope_rejected")


async def _flush_envelopes(
    store: Store,
    event_hub: EventBus,
    conn: Any,
    batcher: IngestBatcher,
    settings: Any,
    metrics: Any,
    websocket: WebSocket,
) -> None:
    queued = batcher.take()
    if not queued:
        return
    outcome = await flush_batch(
        store,
        queued,
        worker_id=conn.worker_id,
        settings=settings,
        metrics=metrics,
    )
    for session_id, last_seq in sorted(
        outcome.acks.items(), key=lambda item: str(item[0])
    ):
        await websocket.send_json(
            CumulativeAck(session_id=session_id, last_seq=last_seq).model_dump(
                mode="json"
            )
        )
    for session_id, body in outcome.wakes:
        await event_hub.publish(session_id, body)


@router.websocket("/internal/worker")
async def worker_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: WorkerHub = websocket.app.state.workers
    store: Store = websocket.app.state.store
    event_hub: EventBus = websocket.app.state.event_hub
    raw_token = _bearer(websocket)
    if raw_token is None or not raw_token.startswith(WORKER_TOKEN_PREFIX):
        hub.observe_protocol("unauthorized")
        await _reject(websocket, "unauthorized", reason="unauthorized")
        return
    token = await authenticate_token(store, raw_token)
    if token is None:
        if await is_revoked_secret(store, raw_token):
            hub.observe_protocol("revoked")
            log.warning("worker token revoked", extra={"event": "worker.auth.revoked"})
            await _reject(websocket, "revoked", reason="revoked")
        else:
            hub.observe_protocol("unauthorized")
            await _reject(websocket, "unauthorized", reason="unauthorized")
        return
    try:
        raw: Any = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except TimeoutError:
        await websocket.close(code=WORKER_CLOSE_CODE)
        return
    except WebSocketDisconnect:
        return
    if not isinstance(raw, dict) or raw.get("type") != "register":
        hub.observe_protocol("invalid_register")
        await _reject(websocket, "register required", reason="register_required")
        return
    try:
        register = parse_register(raw)
    except UnsupportedProtocol:
        hub.observe_protocol("unsupported_protocol")
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
        hub.observe_protocol("invalid_register")
        await _reject(websocket, "invalid register", reason="invalid_register")
        return
    try:
        conn = await register_worker(hub, store, websocket, register, token)
    except TokenBindingError:
        hub.observe_protocol("token_bound")
        log.warning(
            "worker token bound to another worker",
            extra={"event": "worker.auth.bound"},
        )
        await _reject(websocket, "token_bound", reason="token_bound")
        return
    if conn is None:
        hub.observe_protocol("invalid_register")
        await _reject(websocket, "invalid register", reason="invalid_register")
        return
    settings = websocket.app.state.settings
    metrics = websocket.app.state.metrics
    batcher = IngestBatcher(max_messages=settings.worker_ingest_batch_size)
    window = settings.worker_ingest_batch_window.total_seconds()
    try:
        while True:
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
            if message is None or batcher.should_flush(window):
                if len(batcher):
                    await _flush_envelopes(
                        store, event_hub, conn, batcher, settings, metrics, websocket
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
                        store, event_hub, conn, batcher, settings, metrics, websocket
                    )
                continue
            if kind == "ephemeral" and envelope is not None:
                await hub.handle_delta(store, event_hub, conn, envelope)
                continue
            if kind == "garbage":
                _log_garbage(hub, metrics, message)
                continue
                continue
            msg_type = message.get("type")
            if msg_type not in WORKER_IN:
                continue
            if msg_type == "heartbeat":
                if conn.token_id is not None and await token_revoked(
                    store, conn.token_id
                ):
                    hub.observe_protocol("revoked")
                    log.warning(
                        "worker token revoked",
                        extra={
                            "event": "worker.auth.revoked",
                            "worker_id": str(conn.worker_id),
                        },
                    )
                    await websocket.close(code=WORKER_CLOSE_CODE, reason="revoked")
                    return
                await heartbeat_worker(hub, store, conn, message)
                continue
            if msg_type == "lease.ack":
                lease_id = _uuid(message.get("lease_id"))
                command_id = message.get("id")
                if lease_id is None or not isinstance(command_id, str):
                    continue
                if lease_id not in conn.leases:
                    continue
                await hub.ack(lease_id, command_id)
                continue
            if msg_type == "lease.release":
                lease_id = _uuid(message.get("lease_id"))
                session_id = _uuid(message.get("session_id"))
                if (
                    lease_id is None
                    or session_id is None
                    or lease_id not in conn.leases
                ):
                    continue
                async with store.session() as db:
                    row = await get_session_by_lease(db, lease_id)
                if row is None:
                    continue
                await hub.release(store, row.tenant_id, session_id, lease_id)
                continue
            if msg_type == "event":
                lease_id = _uuid(message.get("lease_id"))
                event_type = message.get("event_type")
                data = message.get("data")
                if lease_id is None or not isinstance(event_type, str):
                    continue
                if lease_id not in conn.leases:
                    continue
                payload = data if isinstance(data, dict) else None
                await hub.handle_event(
                    store,
                    event_hub,
                    lease_id=lease_id,
                    event_type=event_type,
                    data=payload,
                )
    except WebSocketDisconnect:
        pass
    finally:
        await hub.detach(conn.worker_id, conn)
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
