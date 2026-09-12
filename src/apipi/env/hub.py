import asyncio
import uuid
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

VERBS = frozenset(
    {"hello", "exec", "read", "write", "edit", "list", "artifact", "ping", "close"}
)


class EnvDisconnected(Exception):
    pass


class RunnerConnection:
    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def request(
        self, type: str, payload: dict[str, Any], timeout: float = 5.0
    ) -> dict[str, Any]:
        if type not in VERBS or type == "hello":
            raise ValueError(type)
        req_id = str(uuid.uuid4())
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send_json({"id": req_id, "type": type, **payload})
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise
        except Exception:
            self._pending.pop(req_id, None)
            raise

    def handle_reply(self, message: dict[str, Any]) -> bool:
        req_id = message.get("id")
        if not isinstance(req_id, str):
            return False
        fut = self._pending.pop(req_id, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(message)
        return True

    def fail_pending(self, exc: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    async def shutdown(self) -> None:
        self.fail_pending(EnvDisconnected())
        if self._ws.client_state == WebSocketState.CONNECTED:
            await self._ws.close()


class EnvironmentHub:
    def __init__(self) -> None:
        self._conns: dict[uuid.UUID, RunnerConnection] = {}

    def get(self, environment_id: uuid.UUID) -> RunnerConnection | None:
        return self._conns.get(environment_id)

    def attach(
        self, environment_id: uuid.UUID, websocket: WebSocket
    ) -> RunnerConnection | None:
        if environment_id in self._conns:
            return None
        conn = RunnerConnection(websocket)
        self._conns[environment_id] = conn
        return conn

    def detach(
        self, environment_id: uuid.UUID, conn: RunnerConnection | None = None
    ) -> None:
        current = self._conns.get(environment_id)
        if current is None:
            return
        if conn is not None and current is not conn:
            return
        del self._conns[environment_id]
        current.fail_pending(EnvDisconnected())

    def connected(self, environment_id: uuid.UUID) -> bool:
        return environment_id in self._conns

    async def call(
        self,
        environment_id: uuid.UUID,
        type: str,
        timeout: float = 5.0,
        **payload: Any,
    ) -> dict[str, Any]:
        conn = self._conns.get(environment_id)
        if conn is None:
            raise EnvDisconnected
        return await conn.request(type, payload, timeout=timeout)

    async def close(self, environment_id: uuid.UUID) -> None:
        conn = self._conns.get(environment_id)
        if conn is None:
            return
        await conn.shutdown()
