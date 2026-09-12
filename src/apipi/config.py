import os
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
IMPLEMENTED_RUN_MODES: frozenset[str] = frozenset({"host", "jail"})

HOST_MODE_WARNING = "APIPI_RUN_MODE=host is not suited for production"
TURN_LOG_ON = "turn log on"
METRICS_ON = "APIPI_METRICS on"
METRICS_OFF = "APIPI_METRICS off"
OTEL_SET = "APIPI_OTEL_ENDPOINT set"
OTEL_UNSET = "APIPI_OTEL_ENDPOINT unset"

_PROMPT_BODY_ENV = frozenset(
    {
        "APIPI_LOG_PROMPTS",
        "APIPI_LOG_PROMPT",
        "APIPI_LOG_COMPLETIONS",
        "APIPI_LOG_COMPLETION",
        "APIPI_LOG_BODIES",
        "APIPI_STORE_PROMPTS",
        "APIPI_STORE_COMPLETIONS",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    }
)
_FLAG_OFF = frozenset({"", "0", "false", "off", "no", "n"})


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


def parse_optional_endpoint(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


IdleTtl = Annotated[timedelta, BeforeValidator(parse_ttl)]
OtelEndpoint = Annotated[str | None, BeforeValidator(parse_optional_endpoint)]


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
    metrics: bool = Field(
        default=False,
        validation_alias=AliasChoices("APIPI_METRICS", "metrics"),
    )
    otel_endpoint: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_OTEL_ENDPOINT", "otel_endpoint"),
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


def _flag_on(value: str) -> bool:
    return value.strip().lower() not in _FLAG_OFF


def reject_prompt_body_logging() -> None:
    for name, value in os.environ.items():
        if name.upper() in _PROMPT_BODY_ENV and _flag_on(value):
            raise ConfigError(f"{name} would store prompt or completion bodies")


def load_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        raise ConfigError(_settings_message(exc)) from exc
    reject_prompt_body_logging()
    return settings


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
        if "metrics" in loc:
            return "APIPI_METRICS must be on or off"
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
    if mode == "jail":
        from apipi.pi.jail import require_jail

        require_jail()
