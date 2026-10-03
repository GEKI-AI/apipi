import asyncio
import logging
import time
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
    TOKEN_BOUND_REASON,
    UNAUTHORIZED_REASON,
    UNSUPPORTED_PROTOCOL_REASON,
    WORKER_CLOSE_CODE,
    RejectMessage,
    UnsupportedProtocol,
    parse_register,
    wire_type,
)
from apipi.services.worker_tokens import (
    WORKER_TOKEN_PREFIX,
    authenticate_token,
    is_revoked_secret,
)
from apipi.store.engine import Store
from apipi.store.repo import clear_worker_api_instance
from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.hub import WorkerHub
from apipi.workerhub.register import TokenBindingError, register_worker
from apipi.workerhub.serve import ConnectionServer
from apipi.workerhub.wire import send_frame

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
    await send_frame(
        websocket,
        RejectMessage(error=error).to_wire(),
        metrics=getattr(websocket.app.state, "metrics", None),
    )
    await websocket.close(code=WORKER_CLOSE_CODE, reason=reason)


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
    except WebSocketDisconnect:
        hub.observe_connect("closed")
        return
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
    conn: WorkerConnection,
) -> None:
    metrics = websocket.app.state.metrics
    opened = time.monotonic()
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
    reason = "error"
    try:
        reason = await ConnectionServer(websocket, hub, store, event_hub, conn).run()
    finally:
        reason = conn.disconnect_reason or reason
        unacked = sum(1 for lease_id in conn.leases if lease_id in hub._unacked)
        leases = len(conn.leases)
        current = await hub.detach(conn.worker_id, conn)
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
        if current:
            try:
                async with store.session() as db:
                    await clear_worker_api_instance(
                        db, conn.worker_id, instance_id=hub.settings.instance_id
                    )
            except Exception:
                log.warning(
                    "worker api instance not cleared",
                    extra={
                        "event": "worker.detach.failed",
                        "error_code": "detach_failed",
                        "worker_id": str(conn.worker_id),
                    },
                    exc_info=True,
                )
