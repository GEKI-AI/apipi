from typing import Any

from apipi.config import Settings

DATABASE_URL = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"


def none_settings() -> Settings:
    return Settings(database_url=DATABASE_URL, run_mode="none")


def none_settings_for(settings: Settings, **overrides: Any) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        **overrides,
    )
