from typing import Any

from starlette.websockets import WebSocket, WebSocketState

from apipi.protocol import WORKER_CLOSE_CODE, WireModel


async def send_wire(websocket: WebSocket, payload: dict[str, Any]) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await websocket.send_json(payload)


async def send_message(websocket: WebSocket, message: WireModel) -> None:
    await send_wire(websocket, message.to_wire())


async def close_socket(websocket: WebSocket, reason: str | None = None) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        if reason is not None:
            await websocket.close(code=WORKER_CLOSE_CODE, reason=reason)
        else:
            await websocket.close()
