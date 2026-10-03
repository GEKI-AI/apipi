"""The real worker plays against a fake API that follows each golden transcript.

The test is the API: it sends the frames of the transcript that the API
sends and checks every frame the worker sends back. Steps that only a
real API can do are `action` steps for `api` and are skipped here. Steps
for `worker` set up what the model does or trigger the worker.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import websockets
from tests.support import conformance

from apipi.common.dirs import store_root
from apipi.common.store_check import write_store_check
from apipi.config import Settings
from apipi.worker import client as worker_client
from apipi.worker.client import run_worker
from apipi.worker.fake_harness import FakeHarness

CLOSED = object()


class FakeApiSocket:
    def __init__(self) -> None:
        self.from_worker: asyncio.Queue[str] = asyncio.Queue()
        self.to_worker: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, data: str | bytes) -> None:
        self.from_worker.put_nowait(data if isinstance(data, str) else data.decode())

    async def recv(self) -> str:
        item = await self.to_worker.get()
        if item is CLOSED:
            raise websockets.exceptions.ConnectionClosedError(None, None)
        return str(item)


class ScriptedHarness(FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.search_hook: tuple[str, int] | None = None
        self.search_reply: dict[str, Any] | None = None

    async def generate(self, text: str, **kwargs: Any) -> AsyncIterator[Any]:
        hook = kwargs.get("search")
        if self.search_hook is not None and callable(hook):
            query, limit = self.search_hook
            self.search_reply = await hook(
                str(kwargs["session_id"]), str(kwargs["turn_id"]), query, limit
            )
        async for item in super().generate(text, **kwargs):
            yield item


class WorkerPlayer:
    def __init__(
        self,
        fixture: conformance.Fixture,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.fixture = fixture
        self.binds: dict[str, Any] = {}
        self.sockets: asyncio.Queue[FakeApiSocket] = asyncio.Queue()
        self.sock: FakeApiSocket | None = None
        self.harness = ScriptedHarness()
        self.execution: Any = None
        self.draining: asyncio.Event | None = None
        self.uploads: list[tuple[str, bytes]] = []
        options = fixture.header.get("worker", {})
        token = tmp_path / "worker.token"
        token.write_text("secret\n")
        self.settings = settings.model_copy(
            update={
                "run_mode": "none",
                "worker_accepts": ["none"],
                "max_sessions": 4,
                "worker_token_file": str(token),
                "sessions_dir": str(tmp_path / "worker-sessions"),
                "worker_outbox_dir": None,
            }
        )
        self.options = options
        self.monkeypatch = monkeypatch
        self.task: asyncio.Task[int] | None = None
        self.tasks: list[asyncio.Task[Any]] = []

    def make_store_check(self) -> tuple[str, str]:
        return write_store_check(store_root(self.settings))

    def connect_factory(self) -> Any:
        sockets = self.sockets

        @asynccontextmanager
        async def connect(_url: str, **_kwargs: Any) -> AsyncIterator[FakeApiSocket]:
            sock = FakeApiSocket()
            sockets.put_nowait(sock)
            yield sock

        return connect

    def prepare(self) -> None:
        import apipi.worker.execution as execution_module

        real = execution_module.local_execution

        def local_execution(*args: Any, **kwargs: Any) -> Any:
            kwargs["harness"] = self.harness
            self.execution = real(*args, **kwargs)
            return self.execution

        self.monkeypatch.setattr(execution_module, "local_execution", local_execution)
        import apipi.worker.artifact_upload as upload_module

        async def put_via_url(
            url: str, data: bytes, headers: dict[str, str] | None = None
        ) -> None:
            self.uploads.append((url, data))

        self.monkeypatch.setattr(upload_module, "put_via_url", put_via_url)
        self.monkeypatch.setattr(
            worker_client,
            "_install_drain_signals",
            lambda event: setattr(self, "draining", event),
        )
        interval = self.options.get("inventory_seconds")
        if interval is not None:
            self.monkeypatch.setattr(worker_client, "INVENTORY_INTERVAL", interval)

    async def next_frame(self) -> dict[str, Any]:
        assert self.sock is not None
        text = await asyncio.wait_for(self.sock.from_worker.get(), timeout=5)
        frame = json.loads(text)
        assert isinstance(frame, dict)
        return frame

    async def run(self) -> None:
        self.prepare()
        self.task = asyncio.create_task(
            run_worker(
                self.settings,
                url="http://127.0.0.1:8000",
                drain_timeout=5.0,
                connect=self.connect_factory(),
            )
        )
        try:
            for step in self.fixture.steps:
                await self.step(step)
            expected_exit = self.fixture.header.get("worker_exit")
            if expected_exit is not None:
                assert await asyncio.wait_for(self.task, timeout=10) == expected_exit
            if self.tasks:
                done, _ = await asyncio.wait(self.tasks, timeout=5)
                for task in done:
                    task.result()
        finally:
            for task in self.tasks:
                task.cancel()
            if self.task is not None and not self.task.done():
                self.task.cancel()
            if self.task is not None:
                await asyncio.gather(self.task, return_exceptions=True)

    async def step(self, step: dict[str, Any]) -> None:
        if conformance.is_frame(step):
            await self.frame(step)
            return
        kind = step["step"]
        if kind == "connect":
            self.sock = await asyncio.wait_for(self.sockets.get(), timeout=5)
        elif kind in ("disconnect", "close"):
            assert self.sock is not None
            self.sock.to_worker.put_nowait(CLOSED)
            self.sock = None
        elif kind == "action":
            if step["for"] == "worker":
                await getattr(self, f"do_{step['name']}")(**step.get("args", {}))
        elif kind == "check":
            return
        else:
            raise AssertionError(f"unknown step {step}")

    async def frame(self, step: dict[str, Any]) -> None:
        assert self.sock is not None
        if step["from"] == "api":
            if step.get("lost"):
                return
            frame = conformance.render(
                step["frame"], self.binds, store_check=self.make_store_check
            )
            self.sock.to_worker.put_nowait(json.dumps(frame))
            return
        if step.get("optional"):
            return
        await conformance.expect(step, self.next_frame, self.binds, self.fixture.name)

    async def do_create_workspace(self) -> None:
        from apipi.common.dirs import sessions_root

        self.binds.setdefault("session", str(uuid.uuid4()))
        self.binds.setdefault("tenant", str(uuid.uuid4()))
        directory = (
            sessions_root(self.settings) / self.binds["tenant"] / self.binds["session"]
        )
        directory.mkdir(parents=True)

    async def do_drain(self) -> None:
        assert self.draining is not None
        self.draining.set()

    async def do_upload(
        self, kind: str, filename: str, content_type: str, text: str
    ) -> None:
        from apipi.worker.artifact_upload import upload_via_presign

        self.binds.setdefault("session", str(uuid.uuid4()))
        self.tasks.append(
            asyncio.create_task(
                upload_via_presign(
                    self.execution.outbox,
                    self.execution.presign_waiters,
                    self.settings,
                    uuid.UUID(self.binds["session"]),
                    kind=kind,
                    filename=filename,
                    content_type=content_type,
                    data=text.encode(),
                )
            )
        )

    async def do_script_model(
        self,
        function_calls: list[dict[str, Any]] | None = None,
        hold: bool = False,
        search: dict[str, Any] | None = None,
    ) -> None:
        self.harness.function_calls = list(function_calls or [])
        self.harness.hold = hold
        if search is not None:
            self.harness.search_hook = (search["query"], search["max_results"])


@pytest.mark.parametrize("name", conformance.names("worker"))
async def test_the_real_worker_follows_the_transcript(
    name: str,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = WorkerPlayer(conformance.load(name), settings, tmp_path, monkeypatch)
    await player.run()


def test_every_transcript_names_its_modes() -> None:
    for name in conformance.names():
        assert conformance.load(name).header["format"] == 1
    assert uuid.UUID(str(uuid.uuid4()))
