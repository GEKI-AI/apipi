import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from apipi.services.runtime import EventHub
from apipi.store.engine import Store
from apipi.store.repo import clear_worker_api_instance, get_session_by_lease
from apipi.worker import (
    WORKER_IN,
    WorkerHub,
    heartbeat_worker,
    register_worker,
)

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


async def _reject(websocket: WebSocket, error: str) -> None:
    await websocket.send_json({"ok": False, "error": error})
    await websocket.close(code=1008)


@router.websocket("/internal/worker")
async def worker_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: WorkerHub = websocket.app.state.workers
    store: Store = websocket.app.state.store
    event_hub: EventHub = websocket.app.state.event_hub
    if not hub.authorized(_bearer(websocket)):
        await _reject(websocket, "unauthorized")
        return
    try:
        raw: Any = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except TimeoutError:
        await websocket.close(code=1008)
        return
    except WebSocketDisconnect:
        return
    if not isinstance(raw, dict) or raw.get("type") != "register":
        await _reject(websocket, "register required")
        return
    conn = await register_worker(hub, store, websocket, raw)
    if conn is None:
        await _reject(websocket, "invalid register")
        return
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            msg_type = message.get("type")
            if msg_type not in WORKER_IN:
                continue
            if msg_type == "heartbeat":
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
