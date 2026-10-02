import json
import logging
import os
import re
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
LogFormat = Literal["json", "text"]
ArtifactStore = Literal["local", "s3"]
S3Addressing = Literal["auto", "path", "virtual"]
UsageStore = Literal["off", "rollups", "turns"]
ModelList = Literal["probe", "turn", "off"]
SandboxSize = Literal["S", "M", "L"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ErrorCodes = Literal["legacy", "specific"]
SearchProviderName = Literal["tavily", "staan"]
TavilyDepth = Literal["basic", "advanced"]
EventBusMode = Literal["auto", "memory", "postgres"]
THINKING_HELP = (
    "APIPI_PI_THINKING must be off, minimal, low, medium, high, xhigh, or max"
)
BUILTIN_RUN_MODES: frozenset[str] = frozenset({"none", "microvm"})
SANDBOX_SIZE_HELP = "APIPI_SANDBOX_DEFAULT_SIZE must be S, M, or L"
WORKER_ACCEPTS_HELP = "APIPI_WORKER_ACCEPTS must be a comma list from none,microvm"

API_ONLY_REMOVED = "APIPI_API_ONLY was removed in 0.14.0; apipi serve is always the API"
VAULT_MASTER_KEY_UNSET = (
    "APIPI_VAULT_MASTER_KEY is unset; using a local default. "
    "Set a 32-byte key in production."
)
SQLITE_WARNING = (
    "SQLite is for one process. Do not share the file across processes or nodes."
)
SEARCH_TIMEOUT_MAX = 25.0
SEARCH_TIMEOUT_MESSAGE = (
    "APIPI_SEARCH_TIMEOUT must be a duration above 0 and at most 25s, like 15s"
)
SEARCH_KEY_REQUIRED = (
    "APIPI_SEARCH_API_KEY is required when APIPI_SEARCH_PROVIDER is set"
)
RUN_MODE_HELP = "APIPI_RUN_MODE must be none, microvm, or package.mod:Class"
USAGE_STORE_OFF = "usage store off"
USAGE_STORE_ROLLUPS = "usage store rollups"
USAGE_STORE_TURNS = "usage store turns"
USAGE_EXPORT_ON = "usage export on"
USAGE_EXPORT_OFF = "usage export off"
PAYLOAD_EXPORT_ON = "payload export on"
PAYLOAD_EXPORT_OFF = "payload export off"
LIFECYCLE_EXPORT_ON = "lifecycle export on"
LIFECYCLE_EXPORT_OFF = "lifecycle export off"
METRICS_ON = "APIPI_METRICS on"
METRICS_OFF = "APIPI_METRICS off"
OTEL_SET = "APIPI_OTEL_ENDPOINT set"
OTEL_UNSET = "APIPI_OTEL_ENDPOINT unset"
OPENAI_API_KEY_IGNORED = (
    "OPENAI_API_KEY is ignored; the request bearer is sent to the model host"
)
LEGACY_WORKER_TOKEN_MESSAGE = (
    "APIPI_WORKER_TOKEN was removed. Create a per-worker token with "
    "`apipi workers token create` and point the worker at it with "
    "APIPI_WORKER_TOKEN_FILE."
)
WORKER_TOKEN_FILE_REQUIRED = (
    "APIPI_WORKER_TOKEN_FILE is required. Create a per-worker token with "
    "`apipi workers token create`, write the printed secret to a file, "
    "and set APIPI_WORKER_TOKEN_FILE to that path."
)


WORKER_DATABASE_URL_MESSAGE = (
    "apipi worker no longer uses DATABASE_URL: unset it on worker hosts, "
    "in the environment and in the config file. "
    "Only the API connects to Postgres; the worker gets everything it needs "
    "over /internal/worker."
)


def reject_worker_database_url() -> None:
    if os.environ.get("DATABASE_URL"):
        raise ConfigError(WORKER_DATABASE_URL_MESSAGE)


def reject_worker_database_url_toml(config_path: str | None = None) -> None:
    """Reject `database_url` in the worker's TOML config file.

    Programmatic `Settings` are untouched: only a value that comes
    from a config file is rejected, so embedding and tests keep
    working."""
    path = resolve_config_path(config_path)
    if path is None:
        return
    if "database_url" in _toml_values(path):
        raise ConfigError(WORKER_DATABASE_URL_MESSAGE)


def reject_legacy_worker_token() -> None:
    if os.environ.get("APIPI_WORKER_TOKEN"):
        raise ConfigError(LEGACY_WORKER_TOKEN_MESSAGE)


def load_worker_token(token_file: str | None) -> str:
    if not token_file:
        raise ConfigError(WORKER_TOKEN_FILE_REQUIRED)
    try:
        text = Path(token_file).read_text()
    except OSError:
        raise ConfigError(
            f"cannot read APIPI_WORKER_TOKEN_FILE: {token_file}"
        ) from None
    token = text.strip()
    if not token:
        raise ConfigError(f"APIPI_WORKER_TOKEN_FILE is empty: {token_file}")
    return token


_log = logging.getLogger("apipi")

_PI_TOML = {
    "command": "pi_command",
    "auto_compact": "pi_auto_compact",
    "thinking": "pi_thinking",
    "compaction_reserve_tokens": "pi_compaction_reserve_tokens",
    "compaction_keep_recent_tokens": "pi_compaction_keep_recent_tokens",
    "system_prompt": "pi_system_prompt",
    "mem_mib": "pi_mem_mib",
    "platform_prompt": "platform_prompt",
    "platform_prompt_additional": "platform_prompt_additional",
    "model_retry_enabled": "model_retry_enabled",
    "model_max_retries": "model_max_retries",
    "model_backoff_base_ms": "model_backoff_base_ms",
    "model_backoff_max_ms": "model_backoff_max_ms",
    "model_timeout_ms": "model_timeout_ms",
    "model_provider_retries": "model_provider_retries",
    "model_retry_after_max_ms": "model_retry_after_max_ms",
}
_PI_PROMPTS = frozenset(
    {
        "identity.none",
        "identity.computer",
        "main.none",
        "main.hosted",
        "additional.none",
        "additional.hosted",
        "capability",
        "browser",
        "mcp_tool",
    }
)
_SANDBOX_TOML = {
    "backend": "run_mode",
    "kernel": "microvm_kernel",
    "rootfs": "microvm_rootfs",
    "default_size": "sandbox_default_size",
    "default_image": "sandbox_default_image",
    "image_source": "image_source",
    "image_store_version": "image_store_version",
    "image_s3_endpoint": "image_s3_endpoint",
    "image_s3_region": "image_s3_region",
    "image_s3_addressing": "image_s3_addressing",
    "images_dir": "images_dir",
    "images": "sandbox_images",
    "eager_boot": "sandbox_eager_boot",
}
_SANDBOX_RESOURCES_TOML = {
    "mem_mib": "microvm_mem_mib",
    "vcpus": "microvm_vcpus",
    "m_mem_mib": "sandbox_m_mem_mib",
    "l_mem_mib": "sandbox_l_mem_mib",
    "l_vcpus": "sandbox_l_vcpus",
    "image_min_vcpus": "sandbox_image_min_vcpus",
}
_SANDBOX_NETWORK_TOML = {
    "egress_allowlist": "microvm_egress_allowlist",
    "egress_hosts": "microvm_egress_hosts",
    "egress_mbit": "microvm_egress_mbit",
}
_SANDBOX_TTL_TOML = {
    "openai_hosted": "sandbox_ttl_openai_hosted",
}
_WORKER_TOML = {
    "accepts": "worker_accepts",
    "outbox_dir": "worker_outbox_dir",
    "outbox_max_messages": "worker_outbox_max_messages",
    "outbox_max_bytes": "worker_outbox_max_bytes",
    "ingest_batch_size": "worker_ingest_batch_size",
    "ingest_batch_window": "worker_ingest_batch_window",
    "client_cert": "worker_client_cert",
    "client_key": "worker_client_key",
    "server_ca": "worker_server_ca",
}
_MCP_TOML = {
    "allow_hosts": "mcp_allow_hosts",
}
_SEARCH_TOML = {
    "provider": "search_provider",
    "base_url": "search_base_url",
    "timeout": "search_timeout",
    "max_results": "search_max_results",
    "tavily_depth": "search_tavily_depth",
    "staan_market": "search_staan_market",
}


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


def default_sqlite_path() -> Path:
    return Path.cwd() / ".apipi" / "apipi.db"


def default_sqlite_url() -> str:
    path = default_sqlite_path().resolve()
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def postgres_url(url: str) -> str:
    scheme = url.split(":", 1)[0]
    if scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ConfigError("DATABASE_URL must be Postgres or SQLite")
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return "postgresql+asyncpg://" + url.removeprefix("postgres://")


def store_url(url: str) -> str:
    scheme = url.split(":", 1)[0]
    if scheme in {"sqlite", "sqlite+aiosqlite"}:
        if url.startswith("sqlite+aiosqlite://"):
            return url
        return "sqlite+aiosqlite://" + url.removeprefix("sqlite://")
    return postgres_url(url)


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
        if value.total_seconds() <= 0:
            return None
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"", "0", "off", "false", "no"}:
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


def parse_model_names(value: object) -> object:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def parse_image_min_vcpus(value: object) -> object:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("APIPI_SANDBOX_IMAGE_MIN_VCPUS must be JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("APIPI_SANDBOX_IMAGE_MIN_VCPUS must be a table")
    image_id = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
    out: dict[str, int] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or image_id.fullmatch(key) is None:
            raise ValueError("APIPI_SANDBOX_IMAGE_MIN_VCPUS keys must be image ids")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValueError(
                "APIPI_SANDBOX_IMAGE_MIN_VCPUS values must be integers >= 1"
            )
        out[key] = raw
    return out


def parse_image_list(value: object) -> object:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


WORKER_ACCEPTS = frozenset({"none", "microvm"})


def parse_worker_accepts(value: object) -> object:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        parts = [str(part).strip().lower() for part in value if str(part).strip()]
    else:
        return value
    seen: list[str] = []
    for part in parts:
        if part not in WORKER_ACCEPTS:
            raise ValueError(WORKER_ACCEPTS_HELP)
        if part not in seen:
            seen.append(part)
    if not seen:
        raise ValueError(WORKER_ACCEPTS_HELP)
    return seen


def parse_model_registry(value: object) -> object:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("APIPI_MODEL_REGISTRY must be JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("model registry must be a table")
    from apipi.worker.pi.model_caps import ModelCapability

    out: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise ValueError("model registry entries must be tables")
        parsed = ModelCapability.model_validate(item)
        out[key] = parsed.model_dump(exclude_none=True)
    return out


def parse_sandbox_size_setting(value: object) -> object:
    if value is None:
        return "S"
    if isinstance(value, str) and not value.strip():
        return "S"
    if isinstance(value, str):
        return value.strip()
    return value


IdleTtl = Annotated[timedelta, BeforeValidator(parse_ttl)]
OptionalTtl = Annotated[timedelta | None, BeforeValidator(parse_optional_ttl)]
ByteSize = Annotated[int, BeforeValidator(parse_bytes)]
OtelEndpoint = Annotated[str | None, BeforeValidator(parse_optional_endpoint)]
ExportUrl = Annotated[str | None, BeforeValidator(parse_export_url)]
HostList = Annotated[str, BeforeValidator(parse_hosts)]
InstanceId = Annotated[str | None, BeforeValidator(parse_instance_id)]
ImageIdList = Annotated[list[str] | None, BeforeValidator(parse_image_list)]
ImageMinVcpus = Annotated[dict[str, int], BeforeValidator(parse_image_min_vcpus)]
ModelNameList = Annotated[list[str], BeforeValidator(parse_model_names)]
SandboxSizeName = Annotated[SandboxSize, BeforeValidator(parse_sandbox_size_setting)]


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


def parse_on_off(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FLAG_OFF
    raise ValueError("must be on or off")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: str = Field(
        default_factory=default_sqlite_url,
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
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
    log_format: LogFormat = Field(
        default="json",
        validation_alias=AliasChoices("APIPI_LOG_FORMAT", "log_format"),
    )
    idle_ttl: IdleTtl = Field(
        default=timedelta(minutes=15),
        validation_alias=AliasChoices("APIPI_IDLE_TTL", "idle_ttl"),
    )
    sandbox_ttl_openai_hosted: OptionalTtl = Field(
        default=timedelta(hours=1),
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_TTL_OPENAI_HOSTED",
            "sandbox_ttl_openai_hosted",
        ),
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
    error_codes: ErrorCodes = Field(
        default="specific",
        validation_alias=AliasChoices("APIPI_ERROR_CODES", "error_codes"),
    )
    worker_token_file: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_TOKEN_FILE", "worker_token_file"),
    )
    worker_client_cert: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_CLIENT_CERT", "worker_client_cert"),
    )
    worker_client_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_CLIENT_KEY", "worker_client_key"),
    )
    worker_server_ca: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_SERVER_CA", "worker_server_ca"),
    )
    vault_master_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_VAULT_MASTER_KEY", "vault_master_key"),
    )
    mcp_allow_hosts: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_MCP_ALLOW_HOSTS", "mcp_allow_hosts"),
    )
    search_provider: SearchProviderName | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SEARCH_PROVIDER", "search_provider"),
    )
    search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SEARCH_API_KEY", "search_api_key"),
    )
    search_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SEARCH_BASE_URL", "search_base_url"),
    )
    search_timeout: IdleTtl = Field(
        default=timedelta(seconds=15),
        validation_alias=AliasChoices("APIPI_SEARCH_TIMEOUT", "search_timeout"),
    )
    search_max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias=AliasChoices("APIPI_SEARCH_MAX_RESULTS", "search_max_results"),
    )
    search_tavily_depth: TavilyDepth = Field(
        default="basic",
        validation_alias=AliasChoices(
            "APIPI_SEARCH_TAVILY_DEPTH", "search_tavily_depth"
        ),
    )
    search_staan_market: str = Field(
        default="en-us",
        min_length=2,
        validation_alias=AliasChoices(
            "APIPI_SEARCH_STAAN_MARKET", "search_staan_market"
        ),
    )
    worker_lease_ttl: IdleTtl = Field(
        default=timedelta(seconds=30),
        validation_alias=AliasChoices("APIPI_WORKER_LEASE_TTL", "worker_lease_ttl"),
    )
    worker_outbox_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_OUTBOX_DIR", "worker_outbox_dir"),
    )
    worker_outbox_max_messages: int = Field(
        default=10_000,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_WORKER_OUTBOX_MAX_MESSAGES", "worker_outbox_max_messages"
        ),
    )
    worker_outbox_max_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1024,
        validation_alias=AliasChoices(
            "APIPI_WORKER_OUTBOX_MAX_BYTES", "worker_outbox_max_bytes"
        ),
    )
    worker_ingest_batch_size: int = Field(
        default=100,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_WORKER_INGEST_BATCH_SIZE", "worker_ingest_batch_size"
        ),
    )
    worker_ingest_batch_window: IdleTtl = Field(
        default=timedelta(milliseconds=50),
        validation_alias=AliasChoices(
            "APIPI_WORKER_INGEST_BATCH_WINDOW", "worker_ingest_batch_window"
        ),
    )
    worker_memory_mb: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("APIPI_WORKER_MEMORY_MB", "worker_memory_mb"),
    )
    api_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_API_URL", "api_url"),
    )
    worker_accepts: Annotated[
        list[str] | None, BeforeValidator(parse_worker_accepts)
    ] = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_ACCEPTS", "worker_accepts"),
    )
    event_bus: EventBusMode = Field(
        default="auto",
        validation_alias=AliasChoices("APIPI_EVENT_BUS", "event_bus"),
    )
    event_bus_fallback_poll: IdleTtl = Field(
        default=timedelta(seconds=3),
        validation_alias=AliasChoices(
            "APIPI_EVENT_BUS_FALLBACK_POLL", "event_bus_fallback_poll"
        ),
    )
    auth: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_AUTH", "auth"),
    )
    auth_cache_ttl: IdleTtl = Field(
        default=timedelta(seconds=30),
        validation_alias=AliasChoices("APIPI_AUTH_CACHE_TTL", "auth_cache_ttl"),
    )
    auth_cache_max: int = Field(
        default=10000,
        ge=0,
        validation_alias=AliasChoices("APIPI_AUTH_CACHE_MAX", "auth_cache_max"),
    )
    authorize: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_AUTHORIZE", "authorize"),
    )
    pi_command: str = Field(
        default="pi",
        validation_alias=AliasChoices("APIPI_PI_COMMAND", "pi_command"),
    )
    pi_auto_compact: bool = Field(
        default=True,
        validation_alias=AliasChoices("APIPI_PI_AUTO_COMPACT", "pi_auto_compact"),
    )
    pi_thinking: ThinkingLevel = Field(
        default="off",
        validation_alias=AliasChoices("APIPI_PI_THINKING", "pi_thinking"),
    )
    pi_compaction_reserve_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_PI_COMPACTION_RESERVE_TOKENS", "pi_compaction_reserve_tokens"
        ),
    )
    pi_compaction_keep_recent_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_PI_COMPACTION_KEEP_RECENT_TOKENS",
            "pi_compaction_keep_recent_tokens",
        ),
    )
    pi_system_prompt: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_PI_SYSTEM_PROMPT", "pi_system_prompt"),
    )
    pi_mem_mib: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("APIPI_PI_MEM_MIB", "pi_mem_mib"),
    )
    platform_prompt: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_PLATFORM_PROMPT", "platform_prompt"),
    )
    platform_name: str = Field(
        default="ApiPi",
        validation_alias=AliasChoices("APIPI_PLATFORM_NAME", "platform_name"),
    )
    platform_prompt_additional: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APIPI_PLATFORM_PROMPT_ADDITIONAL", "platform_prompt_additional"
        ),
    )
    pi_prompts: dict[str, str] = Field(default_factory=dict)
    model_retry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "APIPI_MODEL_RETRY_ENABLED", "model_retry_enabled"
        ),
    )
    model_max_retries: int = Field(
        default=3,
        ge=0,
        validation_alias=AliasChoices("APIPI_MODEL_MAX_RETRIES", "model_max_retries"),
    )
    model_backoff_base_ms: int = Field(
        default=2000,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_MODEL_BACKOFF_BASE_MS", "model_backoff_base_ms"
        ),
    )
    model_backoff_max_ms: int = Field(
        default=30000,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_MODEL_BACKOFF_MAX_MS", "model_backoff_max_ms"
        ),
    )
    model_timeout_ms: int = Field(
        default=120_000,
        ge=1,
        validation_alias=AliasChoices("APIPI_MODEL_TIMEOUT_MS", "model_timeout_ms"),
    )
    model_provider_retries: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_MODEL_PROVIDER_RETRIES", "model_provider_retries"
        ),
    )
    model_retry_after_max_ms: int = Field(
        default=30000,
        ge=0,
        validation_alias=AliasChoices(
            "APIPI_MODEL_RETRY_AFTER_MAX_MS", "model_retry_after_max_ms"
        ),
    )
    sessions_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SESSIONS_DIR", "sessions_dir"),
    )
    metrics: bool = Field(
        default=False,
        validation_alias=AliasChoices("APIPI_METRICS", "metrics"),
    )
    worker_metrics_host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices(
            "APIPI_WORKER_METRICS_HOST", "worker_metrics_host"
        ),
    )
    worker_metrics_port: int = Field(
        default=9091,
        ge=1,
        le=65535,
        validation_alias=AliasChoices(
            "APIPI_WORKER_METRICS_PORT", "worker_metrics_port"
        ),
    )
    guest_sample_interval: OptionalTtl = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_GUEST_SAMPLE_INTERVAL", "guest_sample_interval"
        ),
    )
    otel_endpoint: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_OTEL_ENDPOINT", "otel_endpoint"),
    )
    model_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_BASE_URL", "model_base_url"),
    )
    model_api_key_overwrite: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPENAI_API_KEY_OVERWRITE", "model_api_key_overwrite"
        ),
    )
    forward_models: bool = Field(
        default=True,
        validation_alias=AliasChoices("APIPI_FORWARD_MODELS", "forward_models"),
    )
    model_attribution_headers: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "APIPI_MODEL_ATTRIBUTION_HEADERS", "model_attribution_headers"
        ),
    )
    model_list: ModelList = Field(
        default="probe",
        validation_alias=AliasChoices("APIPI_MODEL_LIST", "model_list"),
    )
    models: ModelNameList = Field(
        default_factory=list,
        validation_alias=AliasChoices("APIPI_MODELS", "models"),
    )
    model_registry: Annotated[dict[str, Any], BeforeValidator(parse_model_registry)] = (
        Field(
            default_factory=dict,
            validation_alias=AliasChoices("APIPI_MODEL_REGISTRY", "model_registry"),
        )
    )
    max_image_bytes: int = Field(
        default=5 * 1024 * 1024,
        validation_alias=AliasChoices("APIPI_MAX_IMAGE_BYTES", "max_image_bytes"),
    )
    max_images: int = Field(
        default=8,
        validation_alias=AliasChoices("APIPI_MAX_IMAGES", "max_images"),
    )
    image_mimes: str = Field(
        default="image/png,image/jpeg,image/webp,image/gif",
        validation_alias=AliasChoices("APIPI_IMAGE_MIMES", "image_mimes"),
    )
    microvm_kernel: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_MICROVM_KERNEL", "microvm_kernel"),
    )
    microvm_rootfs: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_MICROVM_ROOTFS", "microvm_rootfs"),
    )
    image_store_version: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_IMAGE_STORE_VERSION", "image_store_version"
        ),
    )
    image_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_IMAGE_SOURCE", "image_source"),
    )
    image_s3_endpoint: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_IMAGE_S3_ENDPOINT", "image_s3_endpoint"),
    )
    image_s3_region: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_IMAGE_S3_REGION", "image_s3_region"),
    )
    image_s3_addressing: S3Addressing | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_IMAGE_S3_ADDRESSING", "image_s3_addressing"
        ),
    )
    images_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_IMAGES_DIR", "images_dir"),
    )
    sandbox_images: ImageIdList = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_SANDBOX_IMAGES", "sandbox_images"),
    )
    sandbox_default_image: str = Field(
        default="default",
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_DEFAULT_IMAGE", "sandbox_default_image"
        ),
    )
    sandbox_default_size: SandboxSizeName = Field(
        default="S",
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_DEFAULT_SIZE", "sandbox_default_size"
        ),
    )
    microvm_mem_mib: int = Field(
        default=512,
        ge=1,
        validation_alias=AliasChoices("APIPI_MICROVM_MEM_MIB", "microvm_mem_mib"),
    )
    sandbox_m_mem_mib: int = Field(
        default=1024,
        ge=1,
        validation_alias=AliasChoices("APIPI_SANDBOX_M_MEM_MIB", "sandbox_m_mem_mib"),
    )
    sandbox_l_mem_mib: int = Field(
        default=2048,
        ge=1,
        validation_alias=AliasChoices("APIPI_SANDBOX_L_MEM_MIB", "sandbox_l_mem_mib"),
    )
    sandbox_eager_boot: Annotated[bool, BeforeValidator(parse_on_off)] = Field(
        default=False,
        validation_alias=AliasChoices("APIPI_SANDBOX_EAGER_BOOT", "sandbox_eager_boot"),
    )
    sandbox_l_vcpus: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices("APIPI_SANDBOX_L_VCPUS", "sandbox_l_vcpus"),
    )
    sandbox_image_min_vcpus: ImageMinVcpus = Field(
        default_factory=dict,
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_IMAGE_MIN_VCPUS", "sandbox_image_min_vcpus"
        ),
    )
    microvm_vcpus: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("APIPI_MICROVM_VCPUS", "microvm_vcpus"),
    )
    microvm_egress_allowlist: bool = Field(
        default=False,
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
    max_file_bytes: ByteSize = Field(
        default=50 * 1024 * 1024,
        ge=1,
        validation_alias=AliasChoices("APIPI_MAX_FILE_BYTES", "max_file_bytes"),
    )
    artifact_store: ArtifactStore = Field(
        default="local",
        validation_alias=AliasChoices("APIPI_ARTIFACT_STORE", "artifact_store"),
    )
    local_store_dir: str | None = Field(
        default=".apipi/store",
        validation_alias=AliasChoices("APIPI_LOCAL_STORE_DIR", "local_store_dir"),
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
    presign_ttl: IdleTtl = Field(
        default=timedelta(minutes=15),
        validation_alias=AliasChoices("APIPI_PRESIGN_TTL", "presign_ttl"),
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
    usage_sinks: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_USAGE_SINKS", "usage_sinks"),
    )
    payload_sinks: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_PAYLOAD_SINKS", "payload_sinks"),
    )
    lifecycle_export_url: ExportUrl = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_EXPORT_URL", "lifecycle_export_url"
        ),
    )
    lifecycle_export_token: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_EXPORT_TOKEN", "lifecycle_export_token"
        ),
    )
    lifecycle_export_timeout: IdleTtl = Field(
        default=timedelta(seconds=5),
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_EXPORT_TIMEOUT", "lifecycle_export_timeout"
        ),
    )
    lifecycle_sinks: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_LIFECYCLE_SINKS", "lifecycle_sinks"),
    )
    lifecycle_heartbeat: OptionalTtl = Field(
        default=timedelta(seconds=60),
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_HEARTBEAT", "lifecycle_heartbeat"
        ),
    )
    lifecycle_queue: int = Field(
        default=10000,
        ge=1,
        validation_alias=AliasChoices("APIPI_LIFECYCLE_QUEUE", "lifecycle_queue"),
    )
    lifecycle_batch: int = Field(
        default=100,
        ge=1,
        validation_alias=AliasChoices("APIPI_LIFECYCLE_BATCH", "lifecycle_batch"),
    )
    lifecycle_batch_wait: IdleTtl = Field(
        default=timedelta(seconds=1),
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_BATCH_WAIT", "lifecycle_batch_wait"
        ),
    )
    lifecycle_retry_max: IdleTtl = Field(
        default=timedelta(seconds=60),
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_RETRY_MAX", "lifecycle_retry_max"
        ),
    )
    lifecycle_user_id: Literal["raw", "hash", "omit"] = Field(
        default="raw",
        validation_alias=AliasChoices("APIPI_LIFECYCLE_USER_ID", "lifecycle_user_id"),
    )
    lifecycle_user_id_key: OtelEndpoint = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_USER_ID_KEY", "lifecycle_user_id_key"
        ),
    )
    lifecycle_run_modes: HostList = Field(
        default="",
        validation_alias=AliasChoices(
            "APIPI_LIFECYCLE_RUN_MODES", "lifecycle_run_modes"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_worker_token(cls, values: object) -> object:
        if isinstance(values, dict) and (
            "worker_token" in values or "APIPI_WORKER_TOKEN" in values
        ):
            raise ValueError(LEGACY_WORKER_TOKEN_MESSAGE)
        return values

    @model_validator(mode="after")
    def run_mode_known(self) -> Self:
        if os.environ.get("APIPI_WORKER_TOKEN"):
            raise ValueError(LEGACY_WORKER_TOKEN_MESSAGE)
        mode = self.run_mode
        if mode not in BUILTIN_RUN_MODES and ":" not in mode:
            raise ValueError(RUN_MODE_HELP)
        if self.artifact_store == "s3" and not (
            self.s3_bucket and self.s3_bucket.strip()
        ):
            raise ValueError("APIPI_S3_BUCKET is required")
        if self.artifact_store == "local" and (
            self.local_store_dir is None or not self.local_store_dir.strip()
        ):
            raise ValueError(
                "APIPI_ARTIFACT_STORE is local without APIPI_LOCAL_STORE_DIR: "
                "set APIPI_ARTIFACT_STORE=s3, or set APIPI_LOCAL_STORE_DIR to "
                "a store root the API and every worker mounts at the same path"
            )
        try:
            self.database_url = store_url(self.database_url)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        if self.worker_memory_mb is None:
            self.worker_memory_mb = self.max_sessions * self.sandbox_mem_mib(
                self.sandbox_default_size
            )
        if self.vault_master_key is not None and self.vault_master_key.strip():
            from apipi.services.vault_crypto import parse_vault_master_key

            parse_vault_master_key(self.vault_master_key)
        if self.lifecycle_user_id == "hash" and not (
            isinstance(self.lifecycle_user_id_key, str)
            and self.lifecycle_user_id_key.strip()
        ):
            raise ValueError(
                "APIPI_LIFECYCLE_USER_ID_KEY is required when "
                "APIPI_LIFECYCLE_USER_ID=hash"
            )
        if self.search_provider is not None and not (
            self.search_api_key and self.search_api_key.strip()
        ):
            raise ValueError(SEARCH_KEY_REQUIRED)
        if self.search_base_url is not None and not self.search_base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("APIPI_SEARCH_BASE_URL must be an http URL")
        if not 0 < self.search_timeout.total_seconds() <= SEARCH_TIMEOUT_MAX:
            raise ValueError(SEARCH_TIMEOUT_MESSAGE)
        return self

    def sandbox_mem_mib(self, size: str) -> int:
        if size == "M":
            return self.sandbox_m_mem_mib
        if size == "L":
            return self.sandbox_l_mem_mib
        return self.microvm_mem_mib

    def sandbox_vcpus(self, size: str) -> int:
        if size == "L":
            return self.sandbox_l_vcpus
        return self.microvm_vcpus

    def node_memory_mb(self) -> int:
        assert self.worker_memory_mb is not None
        return self.worker_memory_mb

    def sandbox_ttl_for(self, env_type: str | None) -> timedelta | None:
        if env_type == "openai_hosted":
            return self.sandbox_ttl_openai_hosted
        return None

    def pi_idle_ttl_for(self, env_type: str | None) -> timedelta | None:
        if env_type == "openai_hosted":
            return self.sandbox_ttl_openai_hosted
        return self.idle_ttl


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


def _require_table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _map_table(
    table: dict[str, Any],
    mapping: dict[str, str],
    prefix: str,
    *,
    tables: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        if key not in mapping or (isinstance(value, dict) and key not in tables):
            raise ConfigError(f"unknown setting: {prefix}.{key}")
        out[mapping[key]] = value
    return out


def _flatten_pi(table: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        if key == "prompts":
            prompts = _require_table(value, "[pi.prompts]")
            cleaned: dict[str, str] = {}
            for name, text in prompts.items():
                if name not in _PI_PROMPTS:
                    raise ConfigError(f"unknown setting: pi.prompts.{name}")
                if not isinstance(text, str):
                    raise ConfigError(f"pi.prompts.{name} must be a string")
                cleaned[name] = text
            out["pi_prompts"] = cleaned
        elif key in _PI_TOML:
            if isinstance(value, dict):
                raise ConfigError(f"unknown setting: pi.{key}")
            out[_PI_TOML[key]] = value
        else:
            raise ConfigError(f"unknown setting: pi.{key}")
    return out


def _flatten_sandbox(table: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        if key == "resources":
            out.update(
                _map_table(
                    _require_table(value, "[sandbox.resources]"),
                    _SANDBOX_RESOURCES_TOML,
                    "sandbox.resources",
                    tables=frozenset({"image_min_vcpus"}),
                )
            )
        elif key == "network":
            out.update(
                _map_table(
                    _require_table(value, "[sandbox.network]"),
                    _SANDBOX_NETWORK_TOML,
                    "sandbox.network",
                )
            )
        elif key == "ttl":
            out.update(
                _map_table(
                    _require_table(value, "[sandbox.ttl]"),
                    _SANDBOX_TTL_TOML,
                    "sandbox.ttl",
                )
            )
        elif key == "browser":
            browser = dict(_require_table(value, "[sandbox.browser]"))
            for name in sorted(browser):
                _log.warning("unknown [sandbox.browser] key %s is ignored", name)
        elif key in _SANDBOX_TOML:
            if isinstance(value, dict):
                raise ConfigError(f"unknown setting: sandbox.{key}")
            out[_SANDBOX_TOML[key]] = value
        else:
            raise ConfigError(f"unknown setting: sandbox.{key}")
    return out


def _toml_values(path: Path) -> dict[str, Any]:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a table")
    nested: dict[str, Any] = {}
    raw.pop("api_only", None)
    if "worker_token" in raw:
        raise ConfigError(LEGACY_WORKER_TOKEN_MESSAGE)
    if "models" in raw and isinstance(raw["models"], dict):
        nested["model_registry"] = raw.pop("models")
    if "pi" in raw:
        nested.update(_flatten_pi(_require_table(raw.pop("pi"), "[pi]")))
    if "sandbox" in raw:
        nested.update(_flatten_sandbox(_require_table(raw.pop("sandbox"), "[sandbox]")))
    if "worker" in raw:
        nested.update(
            _map_table(
                _require_table(raw.pop("worker"), "[worker]"),
                _WORKER_TOML,
                "worker",
            )
        )
    if "placement" in raw:
        raise ConfigError(
            "[placement] was removed; type=none goes to any worker "
            "whose accepts set contains none"
        )
    if "search" in raw:
        nested.update(
            _map_table(
                _require_table(raw.pop("search"), "[search]"),
                _SEARCH_TOML,
                "search",
            )
        )
    if "mcp" in raw:
        nested.update(
            _map_table(
                _require_table(raw.pop("mcp"), "[mcp]"),
                _MCP_TOML,
                "mcp",
            )
        )
    known = set(Settings.model_fields)
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in nested:
            raise ConfigError(f"cannot set {key} and its [pi] or [sandbox] path")
        if key not in known or isinstance(value, dict):
            raise ConfigError(f"unknown setting: {key}")
        values[key] = value
    values.update(nested)
    return values


class _ExtendSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)


def _warn_removed_agent_versions_env() -> None:
    raw = os.environ.get("APIPI_AGENT_VERSIONS_KEEP")
    if raw:
        _log.warning(
            "APIPI_AGENT_VERSIONS_KEEP was removed; agent versions are gone, "
            "use the agent bundle export for snapshots"
        )


def _warn_removed_env_none_env() -> None:
    if os.environ.get("APIPI_ENV_NONE_PLACEMENT"):
        _log.warning(
            "APIPI_ENV_NONE_PLACEMENT was removed; type=none goes to any "
            "worker whose accepts set contains none"
        )


def _warn_removed_api_only(path: Path | None) -> None:
    in_toml = False
    if path is not None:
        try:
            in_toml = "api_only" in tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            in_toml = False
    if os.environ.get("APIPI_API_ONLY") or in_toml:
        _log.warning(API_ONLY_REMOVED, extra={"event": "config.api_only_removed"})


def load_settings(*, config_path: str | None = None) -> Settings:
    _warn_removed_agent_versions_env()
    _warn_removed_env_none_env()
    reject_legacy_worker_token()
    path = resolve_config_path(config_path)
    values = _toml_values(path) if path is not None else {}
    _warn_removed_api_only(path)
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


def extend_settings(**values: Any) -> Settings:
    try:
        return _ExtendSettings(**values)
    except ValidationError as exc:
        raise ConfigError(_settings_message(exc)) from exc


def _settings_message(exc: ValidationError) -> str:
    for error in exc.errors():
        loc = error.get("loc", ())
        msg = str(error.get("msg", ""))
        if "APIPI_WORKER_TOKEN was removed" in msg:
            return LEGACY_WORKER_TOKEN_MESSAGE
        if "APIPI_S3_BUCKET" in msg:
            return "APIPI_S3_BUCKET is required"
        if "APIPI_VAULT_MASTER_KEY must be 32 bytes" in msg:
            return "APIPI_VAULT_MASTER_KEY must be 32 bytes (base64 or hex)"
        if RUN_MODE_HELP in msg:
            return RUN_MODE_HELP
        if "database_url" in loc:
            return "DATABASE_URL must be Postgres or SQLite"
        if "run_mode" in loc:
            return RUN_MODE_HELP
        if "worker_accepts" in loc or "APIPI_WORKER_ACCEPTS" in loc:
            return WORKER_ACCEPTS_HELP
        if "idle_ttl" in loc or "APIPI_IDLE_TTL" in loc:
            return "APIPI_IDLE_TTL must be like 15m"
        if (
            "sandbox_ttl_openai_hosted" in loc
            or "APIPI_SANDBOX_TTL_OPENAI_HOSTED" in loc
        ):
            return "APIPI_SANDBOX_TTL_OPENAI_HOSTED must be like 15m or 0"
        if "turn_timeout" in loc:
            return "APIPI_TURN_TIMEOUT must be like 15m"
        if "auth_cache_ttl" in loc:
            return "APIPI_AUTH_CACHE_TTL must be like 15m"
        if "metrics" in loc:
            return "APIPI_METRICS must be on or off"
        if "worker_metrics_port" in loc or "APIPI_WORKER_METRICS_PORT" in loc:
            return "APIPI_WORKER_METRICS_PORT must be 1-65535"
        if "guest_sample_interval" in loc or "APIPI_GUEST_SAMPLE_INTERVAL" in loc:
            return "APIPI_GUEST_SAMPLE_INTERVAL must be like 15s or empty"
        if "forward_models" in loc or "APIPI_FORWARD_MODELS" in loc:
            return "APIPI_FORWARD_MODELS must be on or off"
        if "model_list" in loc or "APIPI_MODEL_LIST" in loc:
            return "APIPI_MODEL_LIST must be probe, turn, or off"
        if "pi_auto_compact" in loc or "APIPI_PI_AUTO_COMPACT" in loc:
            return "APIPI_PI_AUTO_COMPACT must be on or off"
        if "pi_thinking" in loc or "APIPI_PI_THINKING" in loc:
            return THINKING_HELP
        if (
            "pi_compaction_reserve_tokens" in loc
            or "APIPI_PI_COMPACTION_RESERVE_TOKENS" in loc
        ):
            return "APIPI_PI_COMPACTION_RESERVE_TOKENS must be 0 or more"
        if (
            "pi_compaction_keep_recent_tokens" in loc
            or "APIPI_PI_COMPACTION_KEEP_RECENT_TOKENS" in loc
        ):
            return "APIPI_PI_COMPACTION_KEEP_RECENT_TOKENS must be 0 or more"
        if "pi_mem_mib" in loc or "APIPI_PI_MEM_MIB" in loc:
            return "APIPI_PI_MEM_MIB must be at least 1"
        if "model_retry_enabled" in loc or "APIPI_MODEL_RETRY_ENABLED" in loc:
            return "APIPI_MODEL_RETRY_ENABLED must be on or off"
        if "model_max_retries" in loc or "APIPI_MODEL_MAX_RETRIES" in loc:
            return "APIPI_MODEL_MAX_RETRIES must be 0 or more"
        if "model_backoff_base_ms" in loc or "APIPI_MODEL_BACKOFF_BASE_MS" in loc:
            return "APIPI_MODEL_BACKOFF_BASE_MS must be 0 or more"
        if "model_backoff_max_ms" in loc or "APIPI_MODEL_BACKOFF_MAX_MS" in loc:
            return "APIPI_MODEL_BACKOFF_MAX_MS must be 0 or more"
        if "model_timeout_ms" in loc or "APIPI_MODEL_TIMEOUT_MS" in loc:
            return "APIPI_MODEL_TIMEOUT_MS must be at least 1"
        if "model_provider_retries" in loc or "APIPI_MODEL_PROVIDER_RETRIES" in loc:
            return "APIPI_MODEL_PROVIDER_RETRIES must be 0 or more"
        if "model_retry_after_max_ms" in loc or "APIPI_MODEL_RETRY_AFTER_MAX_MS" in loc:
            return "APIPI_MODEL_RETRY_AFTER_MAX_MS must be 0 or more"
        if "port" in loc:
            return "APIPI_PORT must be 1-65535"
        if "instance_id" in loc or "APIPI_INSTANCE_ID" in loc:
            return "APIPI_INSTANCE_ID must be short ASCII"
        if "max_sessions_per_tenant" in loc or "APIPI_MAX_SESSIONS_PER_TENANT" in loc:
            return "APIPI_MAX_SESSIONS_PER_TENANT must be at least 1"
        if "max_sessions" in loc:
            return "APIPI_MAX_SESSIONS must be at least 1"
        if "worker_memory_mb" in loc or "APIPI_WORKER_MEMORY_MB" in loc:
            return "APIPI_WORKER_MEMORY_MB must be at least 1"
        if "max_request_bytes" in loc:
            return "APIPI_MAX_REQUEST_BYTES must be like 1MiB"
        if "max_workspace_bytes" in loc:
            return "APIPI_MAX_WORKSPACE_BYTES must be like 1GiB"
        if "max_artifact_bytes" in loc:
            return "APIPI_MAX_ARTIFACT_BYTES must be like 512MiB"
        if "max_file_bytes" in loc:
            return "APIPI_MAX_FILE_BYTES must be like 50MiB"
        if "log_level" in loc:
            return "APIPI_LOG_LEVEL must be debug, info, warning, error, or critical"
        if "log_format" in loc or "APIPI_LOG_FORMAT" in loc:
            return "APIPI_LOG_FORMAT must be json or text"
        if "db_pool_size" in loc:
            return "APIPI_DB_POOL_SIZE must be at least 1"
        if "sandbox_default_size" in loc or "APIPI_SANDBOX_DEFAULT_SIZE" in loc:
            return SANDBOX_SIZE_HELP
        if "microvm_mem_mib" in loc:
            return "APIPI_MICROVM_MEM_MIB must be at least 1"
        if "sandbox_m_mem_mib" in loc or "APIPI_SANDBOX_M_MEM_MIB" in loc:
            return "APIPI_SANDBOX_M_MEM_MIB must be at least 1"
        if "sandbox_l_mem_mib" in loc or "APIPI_SANDBOX_L_MEM_MIB" in loc:
            return "APIPI_SANDBOX_L_MEM_MIB must be at least 1"
        if "sandbox_eager_boot" in loc or "APIPI_SANDBOX_EAGER_BOOT" in loc:
            return "APIPI_SANDBOX_EAGER_BOOT must be on or off"
        if "sandbox_l_vcpus" in loc or "APIPI_SANDBOX_L_VCPUS" in loc:
            return "APIPI_SANDBOX_L_VCPUS must be at least 1"
        if "microvm_vcpus" in loc:
            return "APIPI_MICROVM_VCPUS must be at least 1"
        if "sandbox_image_min_vcpus" in loc or "APIPI_SANDBOX_IMAGE_MIN_VCPUS" in loc:
            return (
                "APIPI_SANDBOX_IMAGE_MIN_VCPUS must be a JSON object of image id "
                "to integer >= 1"
            )
        if "microvm_egress_allowlist" in loc or "APIPI_MICROVM_EGRESS_ALLOWLIST" in loc:
            return "APIPI_MICROVM_EGRESS_ALLOWLIST must be on or off"
        if "microvm_egress_mbit" in loc or "APIPI_MICROVM_EGRESS_MBIT" in loc:
            return "APIPI_MICROVM_EGRESS_MBIT must be at least 1"
        if "artifact_store" in loc:
            return "APIPI_ARTIFACT_STORE must be local or s3"
        if "s3_bucket" in loc or "APIPI_S3_BUCKET" in loc:
            return "APIPI_S3_BUCKET is required"
        if "image_s3_addressing" in loc or "APIPI_IMAGE_S3_ADDRESSING" in loc:
            return "APIPI_IMAGE_S3_ADDRESSING must be auto, path, or virtual"
        if "s3_addressing" in loc:
            return "APIPI_S3_ADDRESSING must be auto, path, or virtual"
        if "presign_ttl" in loc or "APIPI_PRESIGN_TTL" in loc:
            return "APIPI_PRESIGN_TTL must be like 15m"
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
        if "lifecycle_export_url" in loc or "APIPI_LIFECYCLE_EXPORT_URL" in loc:
            return "APIPI_LIFECYCLE_EXPORT_URL must be an http URL"
        if "lifecycle_export_timeout" in loc or "APIPI_LIFECYCLE_EXPORT_TIMEOUT" in loc:
            return "APIPI_LIFECYCLE_EXPORT_TIMEOUT must be like 5s"
        if "lifecycle_heartbeat" in loc or "APIPI_LIFECYCLE_HEARTBEAT" in loc:
            return "APIPI_LIFECYCLE_HEARTBEAT must be like 60s or 0"
        if "lifecycle_queue" in loc or "APIPI_LIFECYCLE_QUEUE" in loc:
            return "APIPI_LIFECYCLE_QUEUE must be at least 1"
        if "lifecycle_batch" in loc or "APIPI_LIFECYCLE_BATCH" in loc:
            return "APIPI_LIFECYCLE_BATCH must be at least 1"
        if "lifecycle_batch_wait" in loc or "APIPI_LIFECYCLE_BATCH_WAIT" in loc:
            return "APIPI_LIFECYCLE_BATCH_WAIT must be like 1s"
        if "lifecycle_retry_max" in loc or "APIPI_LIFECYCLE_RETRY_MAX" in loc:
            return "APIPI_LIFECYCLE_RETRY_MAX must be like 60s"
        if "lifecycle_user_id" in loc or "APIPI_LIFECYCLE_USER_ID" in loc:
            return "APIPI_LIFECYCLE_USER_ID must be raw, hash, or omit"
        if "APIPI_LIFECYCLE_USER_ID_KEY is required" in msg:
            return (
                "APIPI_LIFECYCLE_USER_ID_KEY is required when "
                "APIPI_LIFECYCLE_USER_ID=hash"
            )
        if "vault_master_key" in loc or "APIPI_VAULT_MASTER_KEY" in loc:
            return "APIPI_VAULT_MASTER_KEY must be 32 bytes (base64 or hex)"
        if "APIPI_SEARCH_BASE_URL" in msg:
            return "APIPI_SEARCH_BASE_URL must be an http URL"
        if SEARCH_KEY_REQUIRED in msg:
            return SEARCH_KEY_REQUIRED
        if (
            "APIPI_SEARCH_TIMEOUT" in msg
            or "search_timeout" in loc
            or "APIPI_SEARCH_TIMEOUT" in loc
        ):
            return SEARCH_TIMEOUT_MESSAGE
        if "search_provider" in loc or "APIPI_SEARCH_PROVIDER" in loc:
            return "APIPI_SEARCH_PROVIDER must be tavily or staan"
        if "search_max_results" in loc or "APIPI_SEARCH_MAX_RESULTS" in loc:
            return "APIPI_SEARCH_MAX_RESULTS must be 1-20"
        if "search_tavily_depth" in loc or "APIPI_SEARCH_TAVILY_DEPTH" in loc:
            return "APIPI_SEARCH_TAVILY_DEPTH must be basic or advanced"
        if "search_staan_market" in loc or "APIPI_SEARCH_STAAN_MARKET" in loc:
            return "APIPI_SEARCH_STAAN_MARKET must be a market like en-us"
    return "invalid configuration"


def require_run_mode(mode: str, settings: Settings | None = None) -> None:
    from apipi.worker.pi.isolation import load_isolation

    load_isolation(mode).require(settings)
