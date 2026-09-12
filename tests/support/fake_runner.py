import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from starlette.types import Message


class AsgiWebsocket:
    def __init__(self, app: FastAPI, path: str) -> None:
        self.app = app
        self.path = path
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
            "headers": [(b"host", b"test")],
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

    async def close(self) -> None:
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (TimeoutError, Exception):
                self._task.cancel()


class FakeRunner:
    def __init__(self, app: FastAPI, environment_id: str, key: str) -> None:
        self.app = app
        self.environment_id = environment_id
        self.key = key
        self.files: dict[str, str] = {}
        self.ws = AsgiWebsocket(app, f"/v1/environments/{environment_id}")
        self._task: asyncio.Task[None] | None = None

    def _handle(self, message: dict[str, Any]) -> dict[str, Any]:
        req_id = message.get("id")
        msg_type = message.get("type")
        if msg_type == "ping":
            return {"id": req_id, "ok": True, "type": "pong"}
        if msg_type == "exec":
            return {
                "id": req_id,
                "ok": True,
                "stdout": str(message.get("command", "")),
                "stderr": "",
                "exit_code": 0,
            }
        if msg_type == "read":
            path = str(message.get("path", ""))
            if path not in self.files:
                return {"id": req_id, "ok": False, "error": "not found"}
            return {"id": req_id, "ok": True, "content": self.files[path]}
        if msg_type == "write":
            self.files[str(message.get("path", ""))] = str(message.get("content", ""))
            return {"id": req_id, "ok": True}
        if msg_type == "edit":
            path = str(message.get("path", ""))
            content = self.files.get(path, "")
            old = str(message.get("old_text", ""))
            new = str(message.get("new_text", ""))
            if old not in content:
                return {"id": req_id, "ok": False, "error": "not found"}
            self.files[path] = content.replace(old, new, 1)
            return {"id": req_id, "ok": True}
        if msg_type == "list":
            prefix = str(message.get("path") or "")
            names = [name for name in sorted(self.files) if name.startswith(prefix)]
            return {"id": req_id, "ok": True, "names": names}
        if msg_type == "artifact":
            return {"id": req_id, "ok": True}
        if msg_type == "close":
            return {"id": req_id, "ok": True, "type": "close"}
        return {"id": req_id, "ok": False, "error": "unknown type"}

    async def _serve(self) -> None:
        try:
            while True:
                message = await self.ws.receive_json()
                await self.ws.send_json(self._handle(message))
                if message.get("type") == "close":
                    return
        except (RuntimeError, asyncio.CancelledError):
            return

    async def start(self) -> None:
        accepted = await self.ws.connect()
        if accepted.get("type") != "websocket.accept":
            raise RuntimeError(accepted)
        await self.ws.send_json({"type": "hello", "key": self.key})
        ack = await self.ws.receive_json()
        if not ack.get("ok"):
            raise RuntimeError(ack)
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.ws.close()


@asynccontextmanager
async def connect_runner(
    app: FastAPI, environment_id: str, key: str
) -> AsyncIterator[FakeRunner]:
    runner = FakeRunner(app, environment_id, key)
    await runner.start()
    try:
        yield runner
    finally:
        await runner.stop()
