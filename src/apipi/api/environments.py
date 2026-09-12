import asyncio
import secrets
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from apipi.env.hub import EnvironmentHub, RunnerConnection
from apipi.runtime import EventHub, persist_event, with_env_actions
from apipi.store.engine import Store
from apipi.store.errors import NotFoundError
from apipi.store.repo import (
    get_environment,
    get_session,
    update_environment,
    update_session,
)
from apipi.tokens import hash_token

router = APIRouter()


async def _reject(websocket: WebSocket, error: str) -> None:
    await websocket.send_json({"ok": False, "error": error})
    await websocket.close(code=1008)


async def _mark(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    environment_id: uuid.UUID,
    *,
    status: str,
    event_type: str,
) -> None:
    async with store.session() as db:
        await update_environment(db, tenant_id, environment_id, status=status)
        row = await get_session(db, tenant_id, session_id)
        if row is None:
            return
        if status == "connected":
            actions = with_env_actions([], row.required_actions)
        else:
            actions = with_env_actions(
                [
                    {
                        "type": "environment_connection",
                        "environment_id": str(environment_id),
                    }
                ],
                row.required_actions,
            )
        await update_session(
            db, tenant_id, session_id, changes={"required_actions": actions}
        )
        await persist_event(
            db,
            hub,
            tenant_id,
            session_id,
            type=event_type,
            data={"environment_id": str(environment_id)},
        )


async def _run_socket(websocket: WebSocket, conn: RunnerConnection) -> None:
    while True:
        message = await websocket.receive_json()
        if not isinstance(message, dict):
            continue
        if conn.handle_reply(message):
            continue
        msg_type = message.get("type")
        if msg_type == "ping":
            await websocket.send_json(
                {"id": message.get("id"), "type": "pong", "ok": True}
            )
            continue
        if msg_type == "close":
            return


@router.websocket("/v1/environments/{environment_id}")
async def environment_socket(websocket: WebSocket, environment_id: uuid.UUID) -> None:
    await websocket.accept()
    store: Store = websocket.app.state.store
    event_hub: EventHub = websocket.app.state.event_hub
    env_hub: EnvironmentHub = websocket.app.state.env_hub
    try:
        raw: Any = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except TimeoutError:
        await websocket.close(code=1008)
        return
    except WebSocketDisconnect:
        return
    if not isinstance(raw, dict) or raw.get("type") != "hello":
        await _reject(websocket, "hello required")
        return
    key = raw.get("key")
    if not isinstance(key, str) or key == "":
        await _reject(websocket, "not_found")
        return
    async with store.session() as db:
        env = await get_environment(db, environment_id)
        if env is None or not secrets.compare_digest(env.key_hash, hash_token(key)):
            await _reject(websocket, "not_found")
            return
        tenant_id = env.tenant_id
        session_id = env.session_id
    conn = env_hub.attach(environment_id, websocket)
    if conn is None:
        await _reject(websocket, "connected")
        return
    try:
        await _mark(
            store,
            event_hub,
            tenant_id,
            session_id,
            environment_id,
            status="connected",
            event_type="agent.session.environment.connected",
        )
        await websocket.send_json({"type": "hello", "ok": True})
        await _run_socket(websocket, conn)
    except WebSocketDisconnect:
        pass
    finally:
        env_hub.detach(environment_id, conn)
        with suppress(NotFoundError):
            await _mark(
                store,
                event_hub,
                tenant_id,
                session_id,
                environment_id,
                status="disconnected",
                event_type="agent.session.environment.disconnected",
            )
