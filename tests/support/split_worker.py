"""In-process worker for API tests.

The API and the worker live in the same test process but only talk over
the `/internal/worker` websocket, the same as in production. The API side
is `create_app(...)`; the worker side is a real
`local_execution(harness=..., outbox=...)` driven by the real
`_serve_connection`, connected through `AsgiWebsocket`.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI

from apipi.config import Settings
from tests.support import wire_schema
from tests.support.asgi_websocket import AsgiWebsocket
from tests.support.http import auth


def api_settings_for(settings: Settings, *, batch_window_zero: bool = True) -> Settings:
    """Return API settings derived from a test settings object.

    The ingest batch window is forced to zero (deterministic per-envelope
    ingest) unless `batch_window_zero` is False, which ingest-batching
    tests need to observe real batching behavior.
    """
    update: dict[str, Any] = {}
    if batch_window_zero:
        update["worker_ingest_batch_window"] = timedelta(0)
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

    def __init__(self, ws: AsgiWebsocket, sent: list[str] | None = None) -> None:
        self._ws = ws
        self._sent = sent

    async def send(self, data: str | bytes) -> None:
        text = data if isinstance(data, str) else data.decode()
        wire_schema.record(wire_schema.WORKER_TO_API, text)
        if self._sent is not None:
            self._sent.append(text)
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


def _attached(hub: Any) -> asyncio.Event:
    """Return an event that is set once `hub` attaches the next connection."""
    event = asyncio.Event()
    attach = hub.attach

    async def attach_once(*args: Any, **kwargs: Any) -> Any:
        hub.attach = attach
        result = await attach(*args, **kwargs)
        event.set()
        return result

    hub.attach = attach_once
    return event


class HeldRelease:
    """Hold the API's next `hub.release` until `gate` opens.

    `entered` is set once a `lease.release` reached `hub.release` (the
    lease is out of the connection but not yet cleared), and `settling`
    once a command or a placement waits for that release.
    """

    def __init__(self, hub: Any) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.settling = asyncio.Event()
        release = hub.release
        settle = hub.settle_release

        async def held(*args: Any, **kwargs: Any) -> Any:
            hub.release = release
            self.entered.set()
            await self.gate.wait()
            return await release(*args, **kwargs)

        async def settling(*args: Any, **kwargs: Any) -> bool:
            if hub._releases:
                self.settling.set()
            return await settle(*args, **kwargs)

        hub.release = held
        hub.settle_release = settling


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
        attached: asyncio.Event,
        background: set[asyncio.Task[Any]],
        bus: Any,
        relay: Any | None = None,
        images_patch: Any | None = None,
        session_leases: dict[Any, Any] | None = None,
    ) -> None:
        self.session_leases = session_leases if session_leases is not None else {}
        self.app = app
        self.execution = execution
        self.harness = harness
        self.pool = getattr(execution, "pool", None)
        self.outbox = outbox
        self._ws = ws
        self._serve_task = serve_task
        self._attached = attached
        self._background = background
        self._bus = bus
        self._relay = relay
        self._images_patch = images_patch

    async def wait_ready(self, timeout: float = 10.0) -> None:
        attached = asyncio.create_task(self._attached.wait())
        try:
            await asyncio.wait(
                {attached, self._serve_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            attached.cancel()
        if self._attached.is_set():
            return
        if self._serve_task.done():
            exc = self._serve_task.exception()
            raise AssertionError(f"worker serve exited early: {exc!r}")
        raise AssertionError("worker did not register in time")

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
            if self._relay is not None:
                with _contextlib.suppress(Exception):
                    self._relay.detach()
            if self._images_patch is not None:
                with _contextlib.suppress(Exception):
                    self._images_patch.stop()
            await asyncio.sleep(0)


async def spawn_split_worker(
    app: FastAPI,
    worker_settings: Settings,
    harness: Any,
    token: str,
    *,
    tracing: Any | None = None,
    metrics: Any | None = None,
    pool: Any | None = None,
    sent: list[str] | None = None,
) -> SplitWorker:
    """Start an in-process worker against `app` and return its handle.

    `tracing`/`metrics` are forwarded to the worker-side execution.
    `pool` swaps in a caller-built worker pool (e.g. pre-seeded for
    capacity tests); its lifecycle reporter is wired like the default
    pool. `sent` records raw worker-to-API socket payloads when given.
    """
    from unittest.mock import patch as _patch

    import apipi.worker.client as _hub

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
    from apipi.common.event_bus import InMemoryEventBus
    from apipi.worker.client import _serve_connection
    from apipi.worker.commands import CommandDedupe
    from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
    from apipi.worker.execution import local_execution
    from apipi.worker.lifecycle import OutboxLifecycleReporter
    from apipi.worker.outbox import Outbox

    bus = InMemoryEventBus()
    # Flush deltas immediately: the production batch window lets a fast
    # single-delta turn commit `done` before the relay flushes, and the
    # API then drops the late delta as post-done. Tests need the live
    # delta deterministically; batching behavior itself is covered by
    # the streaming production path, not this fixture.
    relay = DeltaRelay(window=0)
    outbox = Outbox()
    execution = local_execution(
        worker_settings,
        harness=harness,
        hub=LiveRedirectBus(bus, relay),
        outbox=outbox,
        tracing=tracing,
        metrics=metrics,
    )
    if pool is not None:
        # A caller-built pool (never started: PiPool.__init__ only
        # allocates maps). The auto-created pool is empty and inert.
        execution.pool = pool
    execution.pool.lifecycle = OutboxLifecycleReporter(outbox)
    await bus.start()

    background: set[asyncio.Task[Any]] = set()
    background.add(asyncio.create_task(execution.observe_loop()))
    background.add(asyncio.create_task(execution.reap_loop()))
    background.add(asyncio.create_task(execution.reap_workspace_loop()))
    background.add(asyncio.create_task(execution.sandbox_seen_loop()))
    emitter = getattr(getattr(execution, "pool", None), "lifecycle", None)
    if emitter is not None:
        emitter.start()

    attached = _attached(app.state.workers)
    ws = AsgiWebsocket(
        app,
        "/internal/worker",
        headers=[(b"authorization", f"Bearer {token}".encode())],
    )
    await ws.connect()
    sock = _AsgiWorkerSocket(ws, sent)
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
            drain_deadline=None,
            wait=1.0,
            emitter=emitter,
            dedupe=dedupe,
        )
    )
    worker = SplitWorker(
        app,
        execution,
        harness,
        outbox,
        ws,
        serve_task,
        attached,
        background,
        bus,
        relay,
        _images_patch,
        session_leases,
    )
    await worker.wait_ready()
    return worker


@asynccontextmanager
async def serve_split(
    app: FastAPI,
    settings: Settings,
    harness: Any,
    token: str,
    *,
    worker_settings: Settings | None = None,
    tracing: Any | None = None,
    metrics: Any | None = None,
    pool: Any | None = None,
    sent: list[str] | None = None,
) -> AsyncIterator[SplitWorker]:
    """Start a worker for `app` and shut it down deterministically."""
    worker = await spawn_split_worker(
        app,
        worker_settings
        if worker_settings is not None
        else worker_settings_for(settings),
        harness,
        token,
        tracing=tracing,
        metrics=metrics,
        pool=pool,
        sent=sent,
    )
    try:
        yield worker
    finally:
        await worker.aclose()


@asynccontextmanager
async def split_client_for(
    settings: Settings,
    store: Any,
    *,
    harness: Any | None = None,
    token: str,
    worker_settings: Settings | None = None,
    tracing: Any | None = None,
    api_tracing: Any | None = None,
    metrics: Any | None = None,
    pool: Any | None = None,
    sent: list[str] | None = None,
    **app_kwargs: Any,
) -> AsyncIterator[tuple[FastAPI, Any, SplitWorker]]:
    """Build an API app plus an in-process worker and an HTTP client.

    The API and the worker talk only over the worker socket, the same as
    in production. `harness` lands on the worker side. Extra `create_app`
    keyword arguments (`authorize=`, `blobs=`, ...) go to the API side.
    `tracing=` is the worker side and `api_tracing=` the API side; when
    only one is given it is shared by both (single-exporter tests).
    `metrics=`/`pool=`/`sent=` are worker-side only.
    """
    from httpx import ASGITransport, AsyncClient

    from apipi.gateway import create_app
    from apipi.worker.fake_harness import FakeHarness

    api_settings = api_settings_for(settings)
    # `tracing` names the worker side, `api_tracing` the API side. When
    # only one side is given, share it, so single-exporter tests (e.g.
    # otel) keep seeing both sides.
    api_side_tracing = api_tracing
    worker_side_tracing = tracing
    if api_side_tracing is None:
        api_side_tracing = worker_side_tracing
    if worker_side_tracing is None:
        worker_side_tracing = api_side_tracing
    app = create_app(api_settings, store=store, tracing=api_side_tracing, **app_kwargs)
    worker = await spawn_split_worker(
        app,
        worker_settings
        if worker_settings is not None
        else worker_settings_for(settings),
        harness if harness is not None else FakeHarness(),
        token,
        tracing=worker_side_tracing,
        metrics=metrics,
        pool=pool,
        sent=sent,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield app, client, worker
    finally:
        await worker.aclose()


def block_storage(monkeypatch: Any) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("split worker must not construct storage clients")

    import apipi.store.blobs as blobs
    import apipi.store.engine as engine

    monkeypatch.setattr(engine, "create_engine", _boom)
    monkeypatch.setattr(engine, "Store", _boom)
    monkeypatch.setattr(blobs, "object_store", _boom)
    monkeypatch.setattr(blobs, "blob_store", _boom)
    monkeypatch.setattr(blobs, "S3Store", _boom)


async def wait_for_idle(
    client: Any, token: str, session_id: str, timeout: float = 15.0
) -> dict[str, Any]:
    """Poll `GET /v1/agents/sessions/{id}` until status is idle."""
    import asyncio as _asyncio

    headers = auth(token)
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

    headers = auth(token)
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
