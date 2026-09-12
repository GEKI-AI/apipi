from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apipi.api.agents import router as agents_router
from apipi.api.sessions import router as sessions_router
from apipi.config import Settings, load_settings, postgres_url
from apipi.errors import register_exception_handlers
from apipi.runtime import EventHub, FakeHarness
from apipi.store.engine import Store, create_engine


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = False
        if getattr(app.state, "store", None) is None:
            app.state.store = Store(create_engine(postgres_url(resolved.database_url)))
            owned = True
        yield
        if owned:
            await app.state.store.dispose()

    app = FastAPI(title="ApiPi", version="0.0.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.event_hub = EventHub()
    app.state.harness = FakeHarness()
    register_exception_handlers(app)
    app.include_router(sessions_router)
    app.include_router(agents_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
