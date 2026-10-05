import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from apipi.config import Settings
from apipi.protocol import PROTOCOL_VERSION
from apipi.store.engine import Store
from tests.support.asgi_websocket import AsgiWebsocket
from tests.support.config import none_settings_for
from tests.support.http import auth, tenant_of
from tests.support.split_worker import api_settings_for


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

    async def connect(
        self,
        *,
        capacity: int = 1,
        memory_mb: int | None = None,
        run_mode: str | None = "none",
        accepts: list[str] | None = None,
        protocol: int | None = PROTOCOL_VERSION,
        running: list[dict[str, Any]] | None = None,
        features: list[str] | None = None,
    ) -> dict[str, Any]:
        await self.ws.connect()
        register: dict[str, Any] = {"type": "register", "capacity": capacity}
        if protocol is not None:
            register["protocol"] = protocol
        if memory_mb is not None:
            register["memory_mb"] = memory_mb
        if run_mode is not None:
            register["run_mode"] = run_mode
        if accepts is not None:
            register["accepts"] = accepts
        elif run_mode == "microvm":
            register["accepts"] = ["none", "microvm"]
        elif run_mode is not None:
            register["accepts"] = ["none"]
        if running is not None:
            register["running"] = running
        if features is not None:
            register["features"] = features
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

    async def wait_close(self, timeout: float = 5) -> dict[str, Any]:
        return await self.ws.receive_close(timeout=timeout)

    async def close(self) -> None:
        await self.ws.close()


def socket_settings(
    settings: Settings, ttl: timedelta = timedelta(seconds=30), **kw: Any
) -> Settings:
    return api_settings_for(
        none_settings_for(settings, worker_lease_ttl=ttl, **kw).model_copy(
            update={"metrics": True}
        )
    )


def status_envelope(session_id: uuid.UUID, seq: int, **extra: Any) -> dict[str, Any]:
    return {
        "v": 2,
        "session_id": str(session_id),
        "seq": seq,
        "type": "session.status",
        "payload": {"status": "idle", **extra},
    }


async def acquire_lease(
    app: Any, client: AsyncClient, store: Store, worker: FakeWorker, token: str = "t"
) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    tenant_id = tenant_of(token)
    agent = await client.post(
        "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    session_id = uuid.UUID(created.json()["id"])
    await worker.connect()
    command = await app.state.workers.acquire(
        store, tenant_id, session_id, op="turn.cancel"
    )
    assert command is not None
    await worker.receive_json()
    return tenant_id, session_id, command
