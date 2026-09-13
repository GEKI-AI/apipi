import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.api.agents import router as agents_router
from apipi.api.environments import router as environments_router
from apipi.api.sessions import router as sessions_router
from apipi.api.usage import router as usage_router
from apipi.auth import AuthCache, load_authenticate
from apipi.blobs import ArtifactBlobs, blob_store
from apipi.config import Settings, load_settings, postgres_url
from apipi.env.hub import EnvironmentHub
from apipi.errors import error_body, register_exception_handlers
from apipi.metrics import Metrics, mount_metrics
from apipi.otel import Tracing, current_trace_id
from apipi.pi.artifacts import harvest_session, reap_workspace_loop
from apipi.pi.harness import PiHarness
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.request_id import RequestIdMiddleware
from apipi.runtime import EventHub, FakeHarness
from apipi.store.engine import Store, create_engine

_SKIP_CONTEXT = frozenset({"/health", "/metrics"})
_CONTEXT_HEADERS = frozenset(
    {b"x-apipi-instance", b"x-tenant-id", b"x-user-id", b"x-trace-id"}
)


def _ascii_header(value: object) -> bytes | None:
    if value is None:
        return None
    text = str(value).strip()
    if (
        not text
        or len(text) > 512
        or not text.isascii()
        or "\r" in text
        or "\n" in text
    ):
        return None
    return text.encode("ascii")


def _trace_id_from_parent(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1].lower()
    if len(trace_id) != 32 or trace_id == "0" * 32:
        return None
    try:
        int(trace_id, 16)
    except ValueError:
        return None
    return trace_id


def _header_value(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


class InstanceMiddleware:
    def __init__(self, app: ASGIApp, instance_id: str | None) -> None:
        self.app = app
        self.instance_id = instance_id.encode("ascii") if instance_id else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _SKIP_CONTEXT:
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        incoming = _trace_id_from_parent(_header_value(scope, b"traceparent"))
        if incoming is not None:
            state["trace_id"] = incoming

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name not in _CONTEXT_HEADERS
                ]
                extra: list[tuple[bytes, bytes]] = []
                if self.instance_id is not None:
                    extra.append((b"x-apipi-instance", self.instance_id))
                tenant = _ascii_header(state.get("tenant_id"))
                if tenant is not None:
                    extra.append((b"x-tenant-id", tenant))
                user = _ascii_header(state.get("key_id"))
                if user is not None:
                    extra.append((b"x-user-id", user))
                trace = current_trace_id() or state.get("trace_id")
                encoded = _ascii_header(trace) if trace is not None else None
                if encoded is not None:
                    extra.append((b"x-trace-id", encoded))
                headers.extend(extra)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_context)


class MaxBodyMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        length = 0
                    if length > self.max_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content=error_body(
                                "invalid_request",
                                "Request body too large",
                                "payload_too_large",
                            ),
                        )
                        await response(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    harness: FakeHarness | PiHarness | None = None,
    pool: PiPool | None = None,
    tracing: Tracing | None = None,
    blobs: ArtifactBlobs | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else load_settings()
    resolved_pool = pool if pool is not None else PiPool(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = False
        if getattr(app.state, "store", None) is None:
            app.state.store = Store(
                create_engine(
                    postgres_url(resolved.database_url),
                    pool_size=resolved.db_pool_size,
                )
            )
            owned = True
        reap = asyncio.create_task(resolved_pool.reap_loop())
        workspace_reap = asyncio.create_task(
            reap_workspace_loop(resolved, app.state.store, resolved_pool)
        )
        try:
            yield
        finally:
            reap.cancel()
            workspace_reap.cancel()
            await resolved_pool.close()
            current = getattr(app.state, "tracing", None)
            if isinstance(current, Tracing):
                current.shutdown()
            if owned:
                await app.state.store.dispose()

    app = FastAPI(title="ApiPi", version="0.0.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(InstanceMiddleware, instance_id=resolved.instance_id)
    app.add_middleware(MaxBodyMiddleware, max_bytes=resolved.max_request_bytes)
    app.state.settings = resolved
    app.state.metrics = Metrics() if resolved.metrics else None
    if tracing is not None:
        app.state.tracing = tracing
    elif resolved.otel_endpoint:
        app.state.tracing = Tracing(endpoint=resolved.otel_endpoint)
    else:
        app.state.tracing = None
    app.state.store = store
    app.state.mcp_http = {}
    app.state.mcp_stdio = {}
    app.state.authenticate = load_authenticate(resolved.auth)
    app.state.auth_cache = AuthCache(resolved.auth_cache_ttl)
    app.state.event_hub = EventHub()
    app.state.env_hub = EnvironmentHub()
    app.state.pi_pool = resolved_pool
    app.state.harness = harness if harness is not None else PiHarness(resolved_pool)
    app.state.blobs = blobs if blobs is not None else blob_store(resolved)

    async def harvest_killed(session_id: uuid.UUID, proc: PiProc | None) -> None:
        current = app.state.store
        if current is None:
            return
        async with current.session() as db:
            await harvest_session(
                db,
                resolved,
                session_id,
                proc,
                app.state.env_hub,
                sync_workspace=True,
                blobs=app.state.blobs,
            )

    if resolved_pool.on_kill is None:
        resolved_pool.on_kill = harvest_killed
    register_exception_handlers(app)
    app.include_router(sessions_router)
    app.include_router(agents_router)
    app.include_router(environments_router)
    app.include_router(usage_router)
    if isinstance(app.state.metrics, Metrics):
        mount_metrics(app, app.state.metrics)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
