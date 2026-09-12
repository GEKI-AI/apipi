from fastapi import FastAPI

from apipi.config import Settings, load_settings
from apipi.errors import register_exception_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else load_settings()
    app = FastAPI(title="ApiPi", version="0.0.0")
    app.state.settings = resolved
    register_exception_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
