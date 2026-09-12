from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AliasChoices,
    BeforeValidator,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

RunMode = Literal["host", "jail", "microvm"]
IMPLEMENTED_RUN_MODES: frozenset[str] = frozenset({"host"})

HOST_MODE_WARNING = "APIPI_RUN_MODE=host is not suited for production"


class ConfigError(Exception):
    pass


def parse_ttl(value: object) -> object:
    if isinstance(value, timedelta) or not isinstance(value, str):
        return value
    raw = value.strip().lower()
    if raw.endswith("ms"):
        return timedelta(milliseconds=int(raw[:-2]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    if raw.endswith("m"):
        return timedelta(minutes=int(raw[:-1]))
    if raw.endswith("s"):
        return timedelta(seconds=int(raw[:-1]))
    raise ValueError("TTL must be like 15m")


IdleTtl = Annotated[timedelta, BeforeValidator(parse_ttl)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: str
    run_mode: RunMode = Field(
        default="jail",
        validation_alias=AliasChoices("APIPI_RUN_MODE", "run_mode"),
    )
    idle_ttl: IdleTtl = Field(
        default=timedelta(minutes=15),
        validation_alias=AliasChoices("APIPI_IDLE_TTL", "idle_ttl"),
    )
    auth: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_AUTH", "auth"),
    )
    auth_cache_ttl: IdleTtl = Field(
        default=timedelta(seconds=30),
        validation_alias=AliasChoices("APIPI_AUTH_CACHE_TTL", "auth_cache_ttl"),
    )
    example_ui: bool = Field(
        default=False,
        validation_alias=AliasChoices("APIPI_EXAMPLE_UI", "example_ui"),
    )
    pi_command: str = Field(
        default="pi",
        validation_alias=AliasChoices("APIPI_PI_COMMAND", "pi_command"),
    )
    sessions_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SESSIONS_DIR", "sessions_dir"),
    )
    model_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL", "model_base_url"),
    )
    model_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "model_api_key"),
    )

    @model_validator(mode="after")
    def run_mode_known(self) -> Self:
        if self.run_mode not in {"host", "jail", "microvm"}:
            raise ValueError("APIPI_RUN_MODE must be host, jail, or microvm")
        return self


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(_settings_message(exc)) from exc


def _settings_message(exc: ValidationError) -> str:
    for error in exc.errors():
        loc = error.get("loc", ())
        if "database_url" in loc:
            return "DATABASE_URL is required"
        if "run_mode" in loc:
            return "APIPI_RUN_MODE must be host, jail, or microvm"
        if "idle_ttl" in loc:
            return "APIPI_IDLE_TTL must be like 15m"
        if "auth_cache_ttl" in loc:
            return "APIPI_AUTH_CACHE_TTL must be like 15m"
    return "invalid configuration"


def postgres_url(url: str) -> str:
    scheme = url.split(":", 1)[0]
    if scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ConfigError("DATABASE_URL must be Postgres")
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return "postgresql+asyncpg://" + url.removeprefix("postgres://")


def require_run_mode(mode: str) -> None:
    if mode not in {"host", "jail", "microvm"}:
        raise ConfigError("APIPI_RUN_MODE must be host, jail, or microvm")
    if mode not in IMPLEMENTED_RUN_MODES:
        raise ConfigError(f"APIPI_RUN_MODE={mode} is not available")
