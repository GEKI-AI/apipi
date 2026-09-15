from typing import Any

from fastapi import FastAPI

from tests.support.fake_runner import AsgiWebsocket


class FakeWorker:
    def __init__(
        self, app: FastAPI, token: str, *, worker_id: str | None = None
    ) -> None:
        self.app = app
        self.token = token
        self.worker_id = worker_id
        self.ws = AsgiWebsocket(
            app,
            "/internal/worker",
            headers=[(b"authorization", f"Bearer {token}".encode())],
        )
        self.hello: dict[str, Any] | None = None

    async def connect(self, *, capacity: int = 1) -> dict[str, Any]:
        await self.ws.connect()
        register: dict[str, Any] = {"type": "register", "capacity": capacity}
        if self.worker_id is not None:
            register["id"] = self.worker_id
        await self.ws.send_json(register)
        self.hello = await self.ws.receive_json()
        if self.hello.get("ok") and isinstance(self.hello.get("worker_id"), str):
            self.worker_id = str(self.hello["worker_id"])
        return self.hello

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.ws.send_json(data)

    async def receive_json(self, timeout: float = 5) -> dict[str, Any]:
        return await self.ws.receive_json(timeout=timeout)

    async def close(self) -> None:
        await self.ws.close()
