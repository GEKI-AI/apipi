import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apipi.api.agents import router as agents_router
from apipi.api.sessions import router as sessions_router
from apipi.auth import AuthCache, load_authenticate
from apipi.config import Settings, load_settings, postgres_url
from apipi.errors import register_exception_handlers
from apipi.pi.harness import PiHarness
from apipi.pi.pool import PiPool
from apipi.runtime import EventHub, FakeHarness
from apipi.store.engine import Store, create_engine


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    harness: FakeHarness | PiHarness | None = None,
    pool: PiPool | None = None,
) -> FastAPI:
    resolved = settings if settings is not None else load_settings()
    resolved_pool = pool if pool is not None else PiPool(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = False
        if getattr(app.state, "store", None) is None:
            app.state.store = Store(create_engine(postgres_url(resolved.database_url)))
            owned = True
        reap = asyncio.create_task(resolved_pool.reap_loop())
        try:
            yield
        finally:
            reap.cancel()
            await resolved_pool.close()
            if owned:
                await app.state.store.dispose()

    app = FastAPI(title="ApiPi", version="0.0.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.mcp_http = {}
    app.state.authenticate = load_authenticate(resolved.auth)
    app.state.auth_cache = AuthCache(resolved.auth_cache_ttl)
    app.state.event_hub = EventHub()
    app.state.pi_pool = resolved_pool
    app.state.harness = harness if harness is not None else PiHarness(resolved_pool)
    register_exception_handlers(app)
    app.include_router(sessions_router)
    app.include_router(agents_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
