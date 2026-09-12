import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from apipi.api.agents import router as agents_router
from apipi.api.environments import router as environments_router
from apipi.api.sessions import router as sessions_router
from apipi.api.usage import router as usage_router
from apipi.auth import AuthCache, load_authenticate
from apipi.config import Settings, load_settings, postgres_url
from apipi.env.hub import EnvironmentHub
from apipi.errors import error_body, register_exception_handlers
from apipi.metrics import Metrics, mount_metrics
from apipi.otel import Tracing
from apipi.pi.harness import PiHarness
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.request_id import RequestIdMiddleware
from apipi.runtime import EventHub, FakeHarness
from apipi.store.engine import Store, create_engine


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
        try:
            yield
        finally:
            reap.cancel()
            await resolved_pool.close()
            current = getattr(app.state, "tracing", None)
            if isinstance(current, Tracing):
                current.shutdown()
            if owned:
                await app.state.store.dispose()

    app = FastAPI(title="ApiPi", version="0.0.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
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

    async def harvest_killed(session_id: uuid.UUID, proc: PiProc | None) -> None:
        current = app.state.store
        if current is None:
            return
        from apipi.pi.artifacts import harvest_session

        async with current.session() as db:
            await harvest_session(db, resolved, session_id, proc, app.state.env_hub)

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
