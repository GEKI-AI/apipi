import asyncio
import json
from typing import Any

from fastapi import FastAPI
from starlette.types import Message


class AsgiWebsocket:
    def __init__(
        self,
        app: FastAPI,
        path: str,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.app = app
        self.path = path
        self._headers = headers if headers is not None else []
        self._incoming: asyncio.Queue[Message] = asyncio.Queue()
        self._outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> dict[str, Any]:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test"), *self._headers],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
            "extensions": {},
        }

        async def receive() -> Message:
            return await self._incoming.get()

        async def send(message: Message) -> None:
            await self._outgoing.put(message)

        self._task = asyncio.create_task(self.app(scope, receive, send))
        await self._incoming.put({"type": "websocket.connect"})
        raw = await asyncio.wait_for(self._outgoing.get(), timeout=5)
        return dict(raw)

    async def send_json(self, data: dict[str, Any]) -> None:
        await self._incoming.put(
            {"type": "websocket.receive", "text": json.dumps(data)}
        )

    async def receive_json(self, timeout: float = 5) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self._outgoing.get(), timeout=timeout)
            if message["type"] == "websocket.send":
                text = message.get("text")
                if not isinstance(text, str):
                    raw = message.get("bytes")
                    if not isinstance(raw, bytes):
                        raise RuntimeError("empty websocket send")
                    text = raw.decode()
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise RuntimeError("expected object")
                return parsed
            if message["type"] == "websocket.close":
                raise RuntimeError(f"websocket closed {message.get('code')}")

    async def receive_close(self, timeout: float = 5) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self._outgoing.get(), timeout=timeout)
            if message["type"] == "websocket.close":
                return {
                    "code": message.get("code"),
                    "reason": message.get("reason"),
                }
            if message["type"] == "websocket.send":
                continue

    async def close(self) -> None:
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (TimeoutError, Exception):
                self._task.cancel()
