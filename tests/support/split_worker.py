"""In-process split worker for API tests (issue #473 PR 1).

The API and the worker live in the same test process but only talk over
the `/internal/worker` websocket, the same as in production. The API side
is `create_app(api_only=True)`; the worker side is a real
`local_execution(store=None, harness=..., outbox=...)` driven by the real
`_serve_connection`, connected through `AsgiWebsocket`.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI

from apipi.config import Settings
from tests.support.fake_runner import AsgiWebsocket


def api_settings_for(settings: Settings) -> Settings:
    """Return API-only settings derived from a test settings object."""
    update: dict[str, Any] = {
        "api_only": True,
        "worker_ingest_batch_window": timedelta(0),
    }
    if not settings.local_store_dir and settings.sessions_dir:
        update["local_store_dir"] = settings.sessions_dir
    return settings.model_copy(update=update)


def worker_settings_for(settings: Settings) -> Settings:
    """Return worker settings derived from a test settings object.

    The worker accepts every placement so FakeHarness-backed tests for
    both `none` and `microvm` environments run without real sandboxes.
    The register path (`_serve_connection`) does not enforce
    `require_worker_accepts`, so this never triggers the microVM host
    probe.
    """
    if settings.worker_accepts is not None:
        return settings
    return settings.model_copy(update={"worker_accepts": ["none", "microvm"]})


class _AsgiWorkerSocket:
    """Adapt `AsgiWebsocket` to the `send`/`recv` surface `_serve_connection` uses."""

    def __init__(self, ws: AsgiWebsocket) -> None:
        self._ws = ws

    async def send(self, data: str | bytes) -> None:
        text = data if isinstance(data, str) else data.decode()
        await self._ws._incoming.put({"type": "websocket.receive", "text": text})

    async def recv(self) -> str:
        while True:
            message = await self._ws._outgoing.get()
            if message["type"] == "websocket.send":
                text = message.get("text")
                if not isinstance(text, str):
                    raw = message.get("bytes")
                    if not isinstance(raw, bytes):
                        raise RuntimeError("empty websocket send")
                    text = raw.decode()
                return text
            if message["type"] == "websocket.close":
                raise RuntimeError(f"websocket closed {message.get('code')}")
            continue


class SplitWorker:
    """A running in-process worker connected to `app` over the socket."""

    def __init__(
        self,
        app: FastAPI,
        execution: Any,
        harness: Any,
        outbox: Any,
        ws: AsgiWebsocket,
        serve_task: asyncio.Task[Any],
        background: set[asyncio.Task[Any]],
        bus: Any,
        images_patch: Any | None = None,
    ) -> None:
        self.app = app
        self.execution = execution
        self.harness = harness
        self.pool = getattr(execution, "pool", None)
        self.outbox = outbox
        self._ws = ws
        self._serve_task = serve_task
        self._background = background
        self._bus = bus
        self._images_patch = images_patch

    async def wait_ready(self, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                if self.app.state.workers.live() > 0:
                    return
            except Exception:
                pass
            if self._serve_task.done():
                exc = self._serve_task.exception()
                raise AssertionError(f"worker serve exited early: {exc!r}")
            await asyncio.sleep(0.02)
        raise AssertionError("split worker did not register in time")

    async def aclose(self) -> None:
        # Deterministic shutdown: close the worker first, then the
        # socket, then drain the outbox pump (owned by _serve_connection).
        import contextlib as _contextlib

        async with asyncio.timeout(10):
            with _contextlib.suppress(Exception):
                await self.execution.close()
            for task in list(self._background):
                task.cancel()
            self._serve_task.cancel()
            with _contextlib.suppress(asyncio.CancelledError, Exception):
                await self._serve_task
            for task in list(self._background):
                with _contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with _contextlib.suppress(Exception):
                await self._ws.close()
            with _contextlib.suppress(Exception):
                await self._bus.close()
            if self._images_patch is not None:
                with _contextlib.suppress(Exception):
                    self._images_patch.stop()
            await asyncio.sleep(0)


async def spawn_split_worker(
    app: FastAPI,
    worker_settings: Settings,
    harness: Any,
    token: str,
) -> SplitWorker:
    """Start an in-process worker against `app` and return its handle."""
    from unittest.mock import patch as _patch

    import apipi.worker.hub as _hub

    _real_heartbeat_images = _hub._heartbeat_images

    def _test_heartbeat_images(settings: Settings) -> list[dict[str, str]]:
        images = _real_heartbeat_images(settings)
        if not images:
            return [
                {
                    "id": "default",
                    "version": "test",
                    "digest": "test",
                    "min_size": "S",
                }
            ]
        return images

    _images_patch = _patch.object(_hub, "_heartbeat_images", _test_heartbeat_images)
    _images_patch.start()
    from apipi.services.event_bus import InMemoryEventBus
    from apipi.services.lifecycle_export import OutboxLifecycleReporter
    from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
    from apipi.worker.execution import local_execution
    from apipi.worker.hub import CommandDedupe, _serve_connection
    from apipi.worker.outbox import Outbox

    bus = InMemoryEventBus()
    relay = DeltaRelay()
    outbox = Outbox()
    execution = local_execution(
        worker_settings,
        store=None,
        harness=harness,
        hub=LiveRedirectBus(bus, relay),
        outbox=outbox,
    )
    execution.pool.lifecycle = OutboxLifecycleReporter(outbox)
    execution.db_fallback = False
    await bus.start()

    background: set[asyncio.Task[Any]] = set()
    background.add(asyncio.create_task(execution.observe_loop()))
    background.add(asyncio.create_task(execution.reap_loop()))
    background.add(asyncio.create_task(execution.reap_workspace_loop()))
    lifecycle = getattr(execution, "lifecycle_loop", None)
    if lifecycle is not None:
        background.add(asyncio.create_task(lifecycle()))
    seen = getattr(execution, "sandbox_seen_loop", None)
    if seen is not None:
        background.add(asyncio.create_task(seen()))
    emitter = getattr(getattr(execution, "pool", None), "lifecycle", None)
    if emitter is not None:
        emitter.start()

    ws = AsgiWebsocket(
        app,
        "/internal/worker",
        headers=[(b"authorization", f"Bearer {token}".encode())],
    )
    await ws.connect()
    sock = _AsgiWorkerSocket(ws)
    session_leases: dict[Any, Any] = {}
    command_tasks: set[asyncio.Task[None]] = set()
    tasks: set[asyncio.Task[Any]] = set()
    draining = asyncio.Event()
    dedupe = CommandDedupe()
    serve_task = asyncio.create_task(
        _serve_connection(
            worker_settings,
            execution,
            outbox,
            relay,
            sock,
            session_leases,
            command_tasks,
            tasks,
            draining,
            None,
            1.0,
            1.0,
            emitter,
            dedupe,
        )
    )
    worker = SplitWorker(
        app, execution, harness, outbox, ws, serve_task, background, bus, _images_patch
    )
    await worker.wait_ready()
    # Expose the worker-side harness and pool to tests that inspect
    # `app.state.harness` / `app.state.pi_pool` today.
    app.state.harness = harness
    if worker.pool is not None:
        app.state.pi_pool = worker.pool
    return worker


@asynccontextmanager
async def serve_split(
    app: FastAPI,
    settings: Settings,
    harness: Any,
    token: str,
) -> AsyncIterator[SplitWorker]:
    """Start a worker for `app` and shut it down deterministically."""
    worker = await spawn_split_worker(
        app, worker_settings_for(settings), harness, token
    )
    try:
        yield worker
    finally:
        await worker.aclose()


def connect_for_websocket(ws: AsgiWebsocket) -> Any:
    """Build a `run_worker(connect=...)` factory bound to one `AsgiWebsocket`.

    The factory ignores the URL and headers (the socket is already
    authorized) and yields a `send`/`recv` adapter over the ASGI pair.
    """

    @asynccontextmanager
    async def _connect(url: str, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del url, args, kwargs
        yield _AsgiWorkerSocket(ws)

    return _connect


async def asgi_connect_factory(app: FastAPI, token: str) -> Any:
    """Connect one `AsgiWebsocket` to `/internal/worker` and return a factory.

    The websocket handshake runs now, so the factory returned here only
    adapts the already-connected socket for `run_worker(connect=...)`.
    The caller owns the socket and must close it after the worker exits.
    """
    ws = AsgiWebsocket(
        app,
        "/internal/worker",
        headers=[(b"authorization", f"Bearer {token}".encode())],
    )
    await ws.connect()
    return connect_for_websocket(ws), ws


def _envelope_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


async def wait_for_idle(
    client: Any, token: str, session_id: str, timeout: float = 15.0
) -> dict[str, Any]:
    """Poll `GET /v1/agents/sessions/{id}` until status is idle."""
    import asyncio as _asyncio

    headers = {"Authorization": f"Bearer {token}"}
    deadline = _asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while True:
        response = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=headers
        )
        assert response.status_code == 200, response.text
        last = response.json()
        if last.get("status") == "idle":
            return last
        if _asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"session {session_id} not idle: {last}")
        await _asyncio.sleep(0.05)


async def wait_for_event_types(
    client: Any,
    token: str,
    session_id: str,
    *wanted: str,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Poll session events until every `wanted` type is present."""
    import asyncio as _asyncio

    headers = {"Authorization": f"Bearer {token}"}
    deadline = _asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=headers
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        types = {event["type"] for event in data}
        if all(item in types for item in wanted):
            return data
        if _asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"missing {wanted} in {sorted(types)}")
        await _asyncio.sleep(0.05)
