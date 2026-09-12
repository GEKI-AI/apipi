from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str


class ConfigError(Exception):
    pass


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError("DATABASE_URL is required") from exc


def postgres_url(url: str) -> str:
    scheme = url.split(":", 1)[0]
    if scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ConfigError("DATABASE_URL must be Postgres")
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return "postgresql+asyncpg://" + url.removeprefix("postgres://")
