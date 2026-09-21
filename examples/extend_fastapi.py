from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apipi.config import extend_settings
from apipi.gateway import Gateway
from apipi.store.engine import Store, create_engine

settings = extend_settings(
    database_url="sqlite+aiosqlite:///.apipi/extend.db",
    run_mode="none",
)
store = Store(create_engine(settings.database_url))
gateway = Gateway.create(settings, store=store)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await gateway.startup()
    try:
        yield
    finally:
        await gateway.shutdown()


app = FastAPI(lifespan=lifespan)
gateway.configure(app)
app.include_router(gateway.routers.sessions)
app.include_router(gateway.routers.chat)
app.include_router(gateway.routers.vaults)
app.include_router(gateway.routers.files)
app.include_router(gateway.routers.skills)
app.include_router(gateway.routers.agents)
app.include_router(gateway.routers.environments)
app.include_router(gateway.routers.usage)
app.include_router(gateway.routers.models)
app.include_router(gateway.routers.workers)
app.include_router(gateway.routers.health)


@app.get("/ok")
def ok() -> dict[str, str]:
    return {"status": "ok"}
