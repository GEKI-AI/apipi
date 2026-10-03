"""The real API plays against a fake worker that follows each golden transcript.

The test is the worker: it sends the frames of the transcript that the
worker sends and checks every frame the API sends back. Steps that
only a real API can do (create a session, post a message, cancel) are
`action` steps for `api`.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from tests.support import conformance
from tests.support.fake_runner import AsgiWebsocket
from tests.support.split_worker import api_settings_for
from tests.unit.test_blobs import FakeS3

from apipi.common.dirs import store_root
from apipi.common.objects import NS_ARTIFACTS
from apipi.common.store_check import write_store_check
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.search import SearchService
from apipi.store.blobs import S3Store
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import get_session, set_session_lease

TOKEN = "conformance"
TAVILY_BODY = {
    "results": [
        {
            "title": "Pi",
            "url": "https://pi.example/docs",
            "content": "Pi is a small agent loop",
            "published_date": "2026-01-02",
        }
    ],
    "usage": {"credits": 1},
}


class _ListableFakeS3(FakeS3):
    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = str(kwargs.get("Prefix") or "")
        contents = [
            {"Key": key, "Size": len(data)}
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class ApiPlayer:
    def __init__(
        self,
        fixture: conformance.Fixture,
        settings: Settings,
        store: Store,
        worker_secret: str,
        tmp_path: Path,
    ) -> None:
        self.fixture = fixture
        self.store = store
        self.worker_secret = worker_secret
        self.binds: dict[str, Any] = {}
        self.tasks: list[asyncio.Task[Any]] = []
        self.ws: AsgiWebsocket | None = None
        options = fixture.header.get("api", {})
        update_settings: dict[str, Any] = {}
        self.objects: Any = None
        if options.get("artifact_store") == "s3":
            update_settings.update(
                artifact_store="s3",
                s3_bucket="bucket",
                s3_endpoint="https://s3.example",
                s3_region="us-east-1",
            )
        if options.get("search"):
            update_settings.update(search_provider="tavily", search_api_key="key")
        self.settings = api_settings_for(settings.model_copy(update=update_settings))
        kwargs: dict[str, Any] = {}
        if options.get("artifact_store") == "s3":
            self.fake_s3 = _ListableFakeS3()
            self.objects = S3Store(self.settings, client=self.fake_s3)
            kwargs["objects"] = self.objects
        self.app = create_app(self.settings, store=store, **kwargs)
        if options.get("search"):
            self.app.state.search = SearchService(
                store,
                self.app.state.settings,
                client=httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _request: httpx.Response(200, json=TAVILY_BODY)
                    )
                ),
            )
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )

    def make_store_check(self) -> tuple[str, str]:
        return write_store_check(store_root(self.settings))

    async def next_frame(self) -> dict[str, Any]:
        assert self.ws is not None
        return await self.ws.receive_json(timeout=5)

    async def run(self) -> None:
        try:
            for step in self.fixture.steps:
                await self.step(step)
            if self.tasks:
                await asyncio.wait(self.tasks, timeout=5)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.ws is not None:
                await self.ws.close()
            await self.client.aclose()

    async def step(self, step: dict[str, Any]) -> None:
        if conformance.is_frame(step):
            await self.frame(step)
            return
        kind = step["step"]
        if kind == "connect":
            if self.ws is not None:
                await self.ws.close()
            self.ws = AsgiWebsocket(
                self.app,
                "/internal/worker",
                headers=[(b"authorization", f"Bearer {self.worker_secret}".encode())],
            )
            await self.ws.connect()
        elif kind == "disconnect":
            assert self.ws is not None
            await self.ws.close()
            self.ws = None
        elif kind == "close":
            assert self.ws is not None
            closed = await self.ws.receive_close(timeout=5)
            assert closed["code"] == step["code"], closed
            assert closed["reason"] == step.get("reason"), closed
        elif kind == "action":
            if step["for"] == "api":
                await getattr(self, f"do_{step['name']}")(**step.get("args", {}))
        elif kind == "check":
            await getattr(self, f"check_{step['name']}")(**step.get("args", {}))
        else:
            raise AssertionError(f"unknown step {step}")

    async def frame(self, step: dict[str, Any]) -> None:
        assert self.ws is not None
        if step["from"] == "worker":
            frame = conformance.render(
                step["frame"], self.binds, store_check=self.make_store_check
            )
            if step.get("lost"):
                return
            await self.ws.send_json(frame)
            return
        if step.get("optional"):
            return
        await conformance.expect(
            step, self.next_frame, self.binds, f"{self.fixture.name}"
        )

    async def post(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(
            f"/v1/agents/sessions/{self.binds['session']}/events",
            headers=_auth(),
            json=body,
        )

    async def do_create_session(self, tools: list[str] | None = None) -> None:
        specs: list[dict[str, Any]] = []
        for name in tools or []:
            if name == "web_search":
                specs.append({"type": "web_search"})
            else:
                specs.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": name,
                        "parameters": {"type": "object", "properties": {}},
                    }
                )
        agent = await self.client.post(
            "/v1/agents",
            headers=_auth(),
            json={"name": "bot", "model": "test", "tools": specs},
        )
        created = await self.client.post(
            "/v1/agents/sessions",
            headers=_auth(),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        self.binds["session"] = created.json()["id"]
        self.binds["tenant"] = str(uuid.uuid5(uuid.NAMESPACE_URL, hash_token(TOKEN)))

    async def do_start_turn(self, text: str) -> None:
        self.tasks.append(
            asyncio.create_task(
                self.post({"type": "agent.session.input.message", "content": text})
            )
        )

    async def do_submit_tool_result(self, call_id: str, output: str) -> None:
        self.tasks.append(
            asyncio.create_task(
                self.post(
                    {
                        "type": "agent.session.input.tool_result",
                        "turn_id": self.binds["turn"],
                        "call_id": call_id,
                        "success": True,
                        "output": output,
                    }
                )
            )
        )

    async def do_cancel_turn(self) -> None:
        self.tasks.append(
            asyncio.create_task(self.post({"type": "agent.session.input.cancel"}))
        )

    async def do_seed_cursor(self, last_seq: int) -> None:
        async with self.store.session() as db:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == uuid.UUID(self.binds["session"]))
                .values(worker_seq=last_seq)
            )

    async def do_lease_session(self) -> None:
        lease_id = uuid.uuid4()
        async with self.store.session() as db:
            await set_session_lease(
                db,
                uuid.UUID(self.binds["tenant"]),
                uuid.UUID(self.binds["session"]),
                worker_id=uuid.UUID(self.binds["worker"]),
                lease_id=lease_id,
                lease_until=utc_now() + timedelta(seconds=30),
            )
        self.binds["lease"] = str(lease_id)

    async def do_expire_lease(self) -> None:
        async with self.store.session() as db:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == uuid.UUID(self.binds["session"]))
                .values(lease_until=utc_now() - timedelta(seconds=1))
            )
        await self.app.state.workers.expire(self.store, self.app.state.event_hub)

    async def do_store_bytes(self, text: str) -> None:
        data = text.encode()
        if self.objects is not None:
            await self.objects.put(NS_ARTIFACTS, self.binds["object"], data)
            return
        target = store_root(self.settings) / self.binds["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def check_worker_draining(self) -> None:
        worker_id = uuid.UUID(self.binds["worker"])
        for _ in range(100):
            conn = self.app.state.workers.get(worker_id)
            if conn is not None and conn.draining:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the worker is not draining")

    async def check_lease_cleared(self) -> None:
        async with self.store.session() as db:
            row = await get_session(
                db,
                uuid.UUID(self.binds["tenant"]),
                uuid.UUID(self.binds["session"]),
            )
        assert row is not None and row.lease_id is None

    async def do_stop_session(self) -> None:
        self.tasks.append(
            asyncio.create_task(
                self.app.state.execution.teardown(uuid.UUID(self.binds["session"]))
            )
        )


@pytest.mark.parametrize("name", conformance.names("api"))
async def test_the_real_api_follows_the_transcript(
    name: str, settings: Settings, store: Store, worker_secret: str, tmp_path: Path
) -> None:
    player = ApiPlayer(conformance.load(name), settings, store, worker_secret, tmp_path)
    await player.run()


def test_the_transcripts_exist() -> None:
    assert len(conformance.names("api")) >= 6
    json.dumps(conformance.load("register-hello").steps)
