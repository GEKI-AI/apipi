from typing import Any

from starlette.websockets import WebSocket, WebSocketState


async def send_wire(websocket: WebSocket, payload: dict[str, Any]) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await websocket.send_json(payload)


async def close_socket(websocket: WebSocket, reason: str | None = None) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        if reason is not None:
            await websocket.close(code=1008, reason=reason)
        else:
            await websocket.close()
