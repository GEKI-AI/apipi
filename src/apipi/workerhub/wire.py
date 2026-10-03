import json
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

from apipi.protocol import WORKER_CLOSE_CODE, WireModel, wire_type


async def send_frame(
    websocket: WebSocket, payload: dict[str, Any], *, metrics: Any | None = None
) -> None:
    """Send one JSON frame and count it in `apipi_worker_messages_total`."""
    if metrics is not None:
        size = len(json.dumps(payload, separators=(",", ":")))
        metrics.observe_worker_message("out", wire_type(payload), size)
    await websocket.send_json(payload)


async def send_wire(
    websocket: WebSocket, payload: dict[str, Any], *, metrics: Any | None = None
) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await send_frame(websocket, payload, metrics=metrics)


async def send_message(
    websocket: WebSocket, message: WireModel, *, metrics: Any | None = None
) -> None:
    await send_wire(websocket, message.to_wire(), metrics=metrics)


async def close_socket(websocket: WebSocket, reason: str | None = None) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        if reason is not None:
            await websocket.close(code=WORKER_CLOSE_CODE, reason=reason)
        else:
            await websocket.close()
