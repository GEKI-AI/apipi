import os
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AliasChoices,
    BeforeValidator,
    Field,
    ValidationError,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

RunMode = str
LogLevel = Literal["debug", "info", "warning", "error", "critical"]
ArtifactStore = Literal["local", "s3"]
S3Addressing = Literal["auto", "path", "virtual"]
UsageStore = Literal["off", "rollups", "turns"]
BUILTIN_RUN_MODES: frozenset[str] = frozenset({"none", "microvm"})

NONE_MODE_WARNING = "APIPI_RUN_MODE=none is not suited for production"
RUN_MODE_HELP = "APIPI_RUN_MODE must be none, microvm, or package.mod:Class"
USAGE_STORE_OFF = "usage store off"
USAGE_STORE_ROLLUPS = "usage store rollups"
USAGE_STORE_TURNS = "usage store turns"
USAGE_EXPORT_ON = "usage export on"
USAGE_EXPORT_OFF = "usage export off"
PAYLOAD_EXPORT_ON = "payload export on"
PAYLOAD_EXPORT_OFF = "payload export off"
METRICS_ON = "APIPI_METRICS on"
METRICS_OFF = "APIPI_METRICS off"
OTEL_SET = "APIPI_OTEL_ENDPOINT set"
OTEL_UNSET = "APIPI_OTEL_ENDPOINT unset"


def usage_store_log(store: str) -> str:
    if store == "off":
        return USAGE_STORE_OFF
    if store == "rollups":
        return USAGE_STORE_ROLLUPS
    return USAGE_STORE_TURNS


def usage_retention_log(value: timedelta | None) -> str:
    if value is None:
        return "usage retention unset"
    days = value.total_seconds() / 86400
    if days == int(days) and days >= 1:
        return f"usage retention {int(days)}d"
    hours = value.total_seconds() / 3600
    if hours == int(hours) and hours >= 1:
        return f"usage retention {int(hours)}h"
    minutes = value.total_seconds() / 60
    if minutes == int(minutes) and minutes >= 1:
        return f"usage retention {int(minutes)}m"
    return f"usage retention {int(value.total_seconds())}s"


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
_BYTE_UNITS = {
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
}


class ConfigError(Exception):
    pass


class CapacityError(Exception):
    def __init__(self, message: str, *, code: str = "capacity") -> None:
        super().__init__(message)
        self.code = code


class DiskLimitError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def parse_ttl(value: object) -> object:
    if isinstance(value, timedelta) or not isinstance(value, str):
        return value
    raw = value.strip().lower()
    if raw.endswith("ms"):
        return timedelta(milliseconds=int(raw[:-2]))
    if raw.endswith("d"):
        return timedelta(days=int(raw[:-1]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    if raw.endswith("m"):
        return timedelta(minutes=int(raw[:-1]))
    if raw.endswith("s"):
        return timedelta(seconds=int(raw[:-1]))
    raise ValueError("TTL must be like 15m")


def parse_optional_ttl(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str) and not value.strip():
        return None
    return parse_ttl(value)


def parse_export_url(value: object) -> object:
    parsed = parse_optional_endpoint(value)
    if parsed is None or not isinstance(parsed, str):
        return parsed
    raw = parsed.strip()
    if not raw.startswith(("http://", "https://")):
        raise ValueError("APIPI_USAGE_EXPORT_URL must be an http URL")
    return raw


def parse_bytes(value: object) -> object:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return value
    raw = value.strip().lower().replace(" ", "")
    if raw.isdigit():
        return int(raw)
    for suffix, size in sorted(_BYTE_UNITS.items(), key=lambda item: -len(item[0])):
        if raw.endswith(suffix):
            number = raw[: -len(suffix)]
            return int(number) * size
    raise ValueError("size must be like 512M or 1MiB")


def parse_optional_endpoint(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def parse_instance_id(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > 128 or not raw.isascii() or "\r" in raw or "\n" in raw:
        raise ValueError("APIPI_INSTANCE_ID must be short ASCII")
    return raw


def parse_hosts(value: object) -> object:
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return value


IdleTtl = Annotated[timedelta, BeforeValidator(parse_ttl)]
OptionalTtl = Annotated[timedelta | None, BeforeValidator(parse_optional_ttl)]
ByteSize = Annotated[int, BeforeValidator(parse_bytes)]
OtelEndpoint = Annotated[str | None, BeforeValidator(parse_optional_endpoint)]
ExportUrl = Annotated[str | None, BeforeValidator(parse_export_url)]
HostList = Annotated[str, BeforeValidator(parse_hosts)]
InstanceId = Annotated[str | None, BeforeValidator(parse_instance_id)]


class MappingSource(PydanticBaseSettingsSource):
    def __init__(
        self, settings_cls: type[BaseSettings], values: dict[str, Any]
    ) -> None:
        super().__init__(settings_cls)
        self._values = values

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        del field
        if field_name in self._values:
            return self._values[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: str
    run_mode: RunMode = Field(
        default="none",
        validation_alias=AliasChoices("APIPI_RUN_MODE", "run_mode"),
    )
    host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("APIPI_HOST", "host"),
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("APIPI_PORT", "port"),
    )
    instance_id: InstanceId = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_INSTANCE_ID", "instance_id"),
    )
    log_level: LogLevel = Field(
        default="info",
        validation_alias=AliasChoices("APIPI_LOG_LEVEL", "log_level"),
    )
    idle_ttl: IdleTtl = Field(
        default=timedelta(minutes=15),
        validation_alias=AliasChoices("APIPI_IDLE_TTL", "idle_ttl"),
    )
    workspace_ttl: IdleTtl = Field(
        default=timedelta(hours=1),
        validation_alias=AliasChoices("APIPI_WORKSPACE_TTL", "workspace_ttl"),
    )
    max_sessions: int = Field(
        default=32,
        ge=1,
        validation_alias=AliasChoices("APIPI_MAX_SESSIONS", "max_sessions"),
    )
    max_sessions_per_tenant: int = Field(
        default=32,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_MAX_SESSIONS_PER_TENANT", "max_sessions_per_tenant"
        ),
    )
    turn_timeout: IdleTtl = Field(
        default=timedelta(minutes=10),
        validation_alias=AliasChoices("APIPI_TURN_TIMEOUT", "turn_timeout"),
    )
    auth: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_AUTH", "auth"),
    )
    auth_cache_ttl: IdleTtl = Field(
        default=timedelta(seconds=30),
        validation_alias=AliasChoices("APIPI_AUTH_CACHE_TTL", "auth_cache_ttl"),
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
    microvm_kernel: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_MICROVM_KERNEL", "microvm_kernel"),
    )
    microvm_rootfs: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_MICROVM_ROOTFS", "microvm_rootfs"),
    )
    microvm_mem_mib: int = Field(
        default=512,
        ge=1,
        validation_alias=AliasChoices("APIPI_MICROVM_MEM_MIB", "microvm_mem_mib"),
    )
    microvm_vcpus: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("APIPI_MICROVM_VCPUS", "microvm_vcpus"),
    )
    microvm_egress_allowlist: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "APIPI_MICROVM_EGRESS_ALLOWLIST", "microvm_egress_allowlist"
        ),
    )
    microvm_egress_hosts: HostList = Field(
        default="",
        validation_alias=AliasChoices(
            "APIPI_MICROVM_EGRESS_HOSTS", "microvm_egress_hosts"
        ),
    )
    microvm_egress_mbit: int = Field(
        default=50,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_MICROVM_EGRESS_MBIT", "microvm_egress_mbit"
        ),
    )
    db_pool_size: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("APIPI_DB_POOL_SIZE", "db_pool_size"),
    )
    max_request_bytes: ByteSize = Field(
        default=1024 * 1024,
        ge=1,
        validation_alias=AliasChoices("APIPI_MAX_REQUEST_BYTES", "max_request_bytes"),
    )
    max_workspace_bytes: ByteSize = Field(
        default=1024 * 1024 * 1024,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_MAX_WORKSPACE_BYTES", "max_workspace_bytes"
        ),
    )
    max_artifact_bytes: ByteSize = Field(
        default=512 * 1024 * 1024,
        ge=1,
        validation_alias=AliasChoices("APIPI_MAX_ARTIFACT_BYTES", "max_artifact_bytes"),
    )
    artifact_store: ArtifactStore = Field(
        default="local",
        validation_alias=AliasChoices("APIPI_ARTIFACT_STORE", "artifact_store"),
    )
    s3_bucket: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_S3_BUCKET", "s3_bucket"),
    )
    s3_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("APIPI_S3_REGION", "s3_region"),
    )
    s3_prefix: str = Field(
        default="apipi/artifacts",
        validation_alias=AliasChoices("APIPI_S3_PREFIX", "s3_prefix"),
    )
    s3_endpoint: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_S3_ENDPOINT", "s3_endpoint"),
    )
    s3_addressing: S3Addressing = Field(
        default="auto",
        validation_alias=AliasChoices("APIPI_S3_ADDRESSING", "s3_addressing"),
    )
    usage_store: UsageStore = Field(
        default="turns",
        validation_alias=AliasChoices("APIPI_USAGE_STORE", "usage_store"),
    )
    usage_retention: OptionalTtl = Field(
        default=timedelta(days=15),
        validation_alias=AliasChoices("APIPI_USAGE_RETENTION", "usage_retention"),
    )
    usage_export_url: ExportUrl = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_USAGE_EXPORT_URL", "usage_export_url"),
    )
    usage_export_token: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_USAGE_EXPORT_TOKEN", "usage_export_token"),
    )
    usage_export_timeout: IdleTtl = Field(
        default=timedelta(seconds=5),
        validation_alias=AliasChoices(
            "APIPI_USAGE_EXPORT_TIMEOUT", "usage_export_timeout"
        ),
    )
    usage_export_retries: int = Field(
        default=1,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_USAGE_EXPORT_RETRIES", "usage_export_retries"
        ),
    )
    payload_export_url: ExportUrl = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_PAYLOAD_EXPORT_URL", "payload_export_url"),
    )
    payload_export_token: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_PAYLOAD_EXPORT_TOKEN", "payload_export_token"
        ),
    )
    payload_export_timeout: IdleTtl = Field(
        default=timedelta(seconds=5),
        validation_alias=AliasChoices(
            "APIPI_PAYLOAD_EXPORT_TIMEOUT", "payload_export_timeout"
        ),
    )
    payload_export_retries: int = Field(
        default=1,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_PAYLOAD_EXPORT_RETRIES", "payload_export_retries"
        ),
    )

    @model_validator(mode="after")
    def run_mode_known(self) -> Self:
        mode = self.run_mode
        if mode in {"host", "jail"}:
            raise ValueError(f"APIPI_RUN_MODE={mode} is not valid")
        if mode not in BUILTIN_RUN_MODES and ":" not in mode:
            raise ValueError(RUN_MODE_HELP)
        if self.artifact_store == "s3" and not (
            self.s3_bucket and self.s3_bucket.strip()
        ):
            raise ValueError("APIPI_S3_BUCKET is required")
        return self


def _flag_on(value: str) -> bool:
    return value.strip().lower() not in _FLAG_OFF


def reject_prompt_body_logging() -> None:
    for name, value in os.environ.items():
        if name.upper() in _PROMPT_BODY_ENV and _flag_on(value):
            raise ConfigError(f"{name} would store prompt or completion bodies")


def resolve_config_path(explicit: str | None = None) -> Path | None:
    raw = explicit if explicit is not None else os.environ.get("APIPI_CONFIG")
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    cwd = Path("apipi.toml")
    if cwd.is_file():
        return cwd
    return None


def _toml_values(path: Path) -> dict[str, Any]:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a table")
    known = set(Settings.model_fields)
    for key, value in raw.items():
        if key not in known or isinstance(value, dict):
            raise ConfigError(f"unknown setting: {key}")
    return raw


def load_settings(*, config_path: str | None = None) -> Settings:
    path = resolve_config_path(config_path)
    values = _toml_values(path) if path is not None else {}
    env_file = Path(".env") if Path(".env").is_file() else None

    class Loaded(Settings):
        model_config = SettingsConfigDict(
            extra="ignore",
            populate_by_name=True,
            env_file=str(env_file) if env_file is not None else None,
            env_file_encoding="utf-8",
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            sources: list[PydanticBaseSettingsSource] = [
                init_settings,
                env_settings,
            ]
            if env_file is not None:
                sources.append(dotenv_settings)
            if values:
                sources.append(MappingSource(settings_cls, values))
            sources.append(file_secret_settings)
            return tuple(sources)

    try:
        settings = Loaded()
    except ValidationError as exc:
        raise ConfigError(_settings_message(exc)) from exc
    reject_prompt_body_logging()
    return settings


def _settings_message(exc: ValidationError) -> str:
    for error in exc.errors():
        loc = error.get("loc", ())
        msg = str(error.get("msg", ""))
        if "APIPI_S3_BUCKET" in msg:
            return "APIPI_S3_BUCKET is required"
        if "APIPI_RUN_MODE=host is not valid" in msg:
            return "APIPI_RUN_MODE=host is not valid"
        if "APIPI_RUN_MODE=jail is not valid" in msg:
            return "APIPI_RUN_MODE=jail is not valid"
        if RUN_MODE_HELP in msg:
            return RUN_MODE_HELP
        if "database_url" in loc:
            return "DATABASE_URL is required"
        if "run_mode" in loc:
            return RUN_MODE_HELP
        if "idle_ttl" in loc or "APIPI_IDLE_TTL" in loc:
            return "APIPI_IDLE_TTL must be like 15m"
        if "workspace_ttl" in loc or "APIPI_WORKSPACE_TTL" in loc:
            return "APIPI_WORKSPACE_TTL must be like 15m"
        if "turn_timeout" in loc:
            return "APIPI_TURN_TIMEOUT must be like 15m"
        if "auth_cache_ttl" in loc:
            return "APIPI_AUTH_CACHE_TTL must be like 15m"
        if "metrics" in loc:
            return "APIPI_METRICS must be on or off"
        if "port" in loc:
            return "APIPI_PORT must be 1-65535"
        if "instance_id" in loc or "APIPI_INSTANCE_ID" in loc:
            return "APIPI_INSTANCE_ID must be short ASCII"
        if "max_sessions_per_tenant" in loc or "APIPI_MAX_SESSIONS_PER_TENANT" in loc:
            return "APIPI_MAX_SESSIONS_PER_TENANT must be at least 1"
        if "max_sessions" in loc:
            return "APIPI_MAX_SESSIONS must be at least 1"
        if "max_request_bytes" in loc:
            return "APIPI_MAX_REQUEST_BYTES must be like 1MiB"
        if "max_workspace_bytes" in loc:
            return "APIPI_MAX_WORKSPACE_BYTES must be like 1GiB"
        if "max_artifact_bytes" in loc:
            return "APIPI_MAX_ARTIFACT_BYTES must be like 512MiB"
        if "log_level" in loc:
            return "APIPI_LOG_LEVEL must be debug, info, warning, error, or critical"
        if "db_pool_size" in loc:
            return "APIPI_DB_POOL_SIZE must be at least 1"
        if "microvm_mem_mib" in loc:
            return "APIPI_MICROVM_MEM_MIB must be at least 1"
        if "microvm_vcpus" in loc:
            return "APIPI_MICROVM_VCPUS must be at least 1"
        if "microvm_egress_allowlist" in loc or "APIPI_MICROVM_EGRESS_ALLOWLIST" in loc:
            return "APIPI_MICROVM_EGRESS_ALLOWLIST must be on or off"
        if "microvm_egress_mbit" in loc or "APIPI_MICROVM_EGRESS_MBIT" in loc:
            return "APIPI_MICROVM_EGRESS_MBIT must be at least 1"
        if "artifact_store" in loc:
            return "APIPI_ARTIFACT_STORE must be local or s3"
        if "s3_bucket" in loc or "APIPI_S3_BUCKET" in loc:
            return "APIPI_S3_BUCKET is required"
        if "s3_addressing" in loc:
            return "APIPI_S3_ADDRESSING must be auto, path, or virtual"
        if "usage_store" in loc or "APIPI_USAGE_STORE" in loc:
            return "APIPI_USAGE_STORE must be off, rollups, or turns"
        if "usage_retention" in loc or "APIPI_USAGE_RETENTION" in loc:
            return "APIPI_USAGE_RETENTION must be like 15d"
        if "usage_export_url" in loc or "APIPI_USAGE_EXPORT_URL" in loc:
            return "APIPI_USAGE_EXPORT_URL must be an http URL"
        if "usage_export_timeout" in loc or "APIPI_USAGE_EXPORT_TIMEOUT" in loc:
            return "APIPI_USAGE_EXPORT_TIMEOUT must be like 15m"
        if "usage_export_retries" in loc or "APIPI_USAGE_EXPORT_RETRIES" in loc:
            return "APIPI_USAGE_EXPORT_RETRIES must be at least 0"
        if "payload_export_url" in loc or "APIPI_PAYLOAD_EXPORT_URL" in loc:
            return "APIPI_PAYLOAD_EXPORT_URL must be an http URL"
        if "payload_export_timeout" in loc or "APIPI_PAYLOAD_EXPORT_TIMEOUT" in loc:
            return "APIPI_PAYLOAD_EXPORT_TIMEOUT must be like 15m"
        if "payload_export_retries" in loc or "APIPI_PAYLOAD_EXPORT_RETRIES" in loc:
            return "APIPI_PAYLOAD_EXPORT_RETRIES must be at least 0"
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


def require_run_mode(mode: str, settings: Settings | None = None) -> None:
    from apipi.pi.isolation import load_isolation

    load_isolation(mode).require(settings)
