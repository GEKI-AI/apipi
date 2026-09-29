import json
import logging
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
LogFormat = Literal["json", "text"]
ArtifactStore = Literal["local", "s3"]
S3Addressing = Literal["auto", "path", "virtual"]
UsageStore = Literal["off", "rollups", "turns"]
ModelList = Literal["probe", "turn", "off"]
MicrovmImage = Literal["default", "browser", "work"]
SandboxSize = Literal["S", "M", "L"]
EnvNonePlacement = Literal["chat", "microvm", "reject"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ErrorCodes = Literal["legacy", "specific"]
THINKING_HELP = (
    "APIPI_PI_THINKING must be off, minimal, low, medium, high, xhigh, or max"
)
BUILTIN_RUN_MODES: frozenset[str] = frozenset({"none", "chat", "microvm"})
MICROVM_IMAGE_HELP = "APIPI_MICROVM_IMAGE must be default, browser, or work"
SANDBOX_SIZE_HELP = "APIPI_SANDBOX_DEFAULT_SIZE must be S, M, or L"
ENV_NONE_PLACEMENT_HELP = "APIPI_ENV_NONE_PLACEMENT must be chat, microvm, or reject"

NONE_MODE_WARNING = "APIPI_RUN_MODE=none is not suited for production"
VAULT_MASTER_KEY_UNSET = (
    "APIPI_VAULT_MASTER_KEY is unset; using a local default. "
    "Set a 32-byte key in production."
)
CHAT_MODE_NOTE = "APIPI_RUN_MODE=chat runs Pi on the host without a microVM"
SQLITE_WARNING = (
    "SQLite is for one process. Do not share the file across processes or nodes."
)
RUN_MODE_HELP = "APIPI_RUN_MODE must be none, chat, microvm, or package.mod:Class"
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
FLAT_TOML_WARNING = "TOML key {key} is deprecated; use {path}"

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
_SANDBOX_TOML = {
    "backend": "run_mode",
    "kernel": "microvm_kernel",
    "rootfs": "microvm_rootfs",
    "rootfs_browser": "microvm_rootfs_browser",
    "image": "microvm_image",
    "default_size": "sandbox_default_size",
    "default_image": "sandbox_default_image",
    "image_source": "image_source",
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
}
_SANDBOX_NETWORK_TOML = {
    "egress_allowlist": "microvm_egress_allowlist",
    "egress_hosts": "microvm_egress_hosts",
    "egress_mbit": "microvm_egress_mbit",
}
_SANDBOX_TTL_TOML = {
    "openai_hosted": "workspace_ttl",
    "self_hosted": "sandbox_ttl_self_hosted",
}
_SANDBOX_BROWSER_TOML = {
    "auto_playwright": "sandbox_auto_playwright",
}
_PLACEMENT_TOML = {
    "env_none": "env_none_placement",
}
_LEGACY_FLAT_TOML = {
    "run_mode": "[sandbox].backend",
    "pi_command": "[pi].command",
    "pi_auto_compact": "[pi].auto_compact",
    "microvm_kernel": "[sandbox].kernel",
    "microvm_rootfs": "[sandbox].rootfs",
    "microvm_mem_mib": "[sandbox.resources].mem_mib",
    "microvm_vcpus": "[sandbox.resources].vcpus",
    "microvm_egress_allowlist": "[sandbox.network].egress_allowlist",
    "microvm_egress_hosts": "[sandbox.network].egress_hosts",
    "microvm_egress_mbit": "[sandbox.network].egress_mbit",
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


def parse_image_list(value: object) -> object:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


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


def parse_microvm_image(value: object) -> object:
    if value is None:
        return "default"
    if isinstance(value, str) and not value.strip():
        return "default"
    if isinstance(value, str):
        return value.strip()
    return value


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
MicrovmImageName = Annotated[MicrovmImage, BeforeValidator(parse_microvm_image)]
ImageIdList = Annotated[list[str] | None, BeforeValidator(parse_image_list)]
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
    workspace_ttl: OptionalTtl = Field(
        default=timedelta(hours=1),
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_TTL_OPENAI_HOSTED",
            "sandbox_ttl_openai_hosted",
            "APIPI_WORKSPACE_TTL",
            "workspace_ttl",
        ),
    )
    sandbox_ttl_self_hosted: OptionalTtl = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_TTL_SELF_HOSTED", "sandbox_ttl_self_hosted"
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
        default="legacy",
        validation_alias=AliasChoices("APIPI_ERROR_CODES", "error_codes"),
    )
    worker_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_TOKEN", "worker_token"),
    )
    vault_master_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_VAULT_MASTER_KEY", "vault_master_key"),
    )
    worker_lease_ttl: IdleTtl = Field(
        default=timedelta(seconds=30),
        validation_alias=AliasChoices("APIPI_WORKER_LEASE_TTL", "worker_lease_ttl"),
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
    api_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("APIPI_API_ONLY", "api_only"),
    )
    env_none_placement: EnvNonePlacement = Field(
        default="chat",
        validation_alias=AliasChoices("APIPI_ENV_NONE_PLACEMENT", "env_none_placement"),
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
    microvm_rootfs_browser: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "APIPI_MICROVM_ROOTFS_BROWSER", "microvm_rootfs_browser"
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
    microvm_image: MicrovmImageName = Field(
        default="default",
        validation_alias=AliasChoices("APIPI_MICROVM_IMAGE", "microvm_image"),
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
    sandbox_auto_playwright: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "APIPI_SANDBOX_AUTO_PLAYWRIGHT", "sandbox_auto_playwright"
        ),
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
    agent_versions_keep: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices(
            "APIPI_AGENT_VERSIONS_KEEP", "agent_versions_keep"
        ),
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
        memory = self.worker_memory_mb
        if memory is None:
            return self.max_sessions * self.sandbox_mem_mib(self.sandbox_default_size)
        return memory

    def sandbox_ttl_for(self, env_type: str | None) -> timedelta | None:
        if env_type in {"openai_hosted", "hosted"}:
            return self.workspace_ttl
        if env_type == "self_hosted":
            return self.sandbox_ttl_self_hosted
        return None

    def pi_idle_ttl_for(self, env_type: str | None) -> timedelta | None:
        if env_type in {"openai_hosted", "hosted"}:
            return self.workspace_ttl
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
    table: dict[str, Any], mapping: dict[str, str], prefix: str
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in table.items():
        if key not in mapping or isinstance(value, dict):
            raise ConfigError(f"unknown setting: {prefix}.{key}")
        out[mapping[key]] = value
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
            if "playwright_mcp" in browser:
                _log.warning("playwright_mcp was removed and is ignored")
                browser.pop("playwright_mcp")
            out.update(_map_table(browser, _SANDBOX_BROWSER_TOML, "sandbox.browser"))
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
    if "models" in raw and isinstance(raw["models"], dict):
        nested["model_registry"] = raw.pop("models")
    if "pi" in raw:
        nested.update(_map_table(_require_table(raw.pop("pi"), "[pi]"), _PI_TOML, "pi"))
    if "sandbox" in raw:
        nested.update(_flatten_sandbox(_require_table(raw.pop("sandbox"), "[sandbox]")))
    if "placement" in raw:
        nested.update(
            _map_table(
                _require_table(raw.pop("placement"), "[placement]"),
                _PLACEMENT_TOML,
                "placement",
            )
        )
    known = set(Settings.model_fields)
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in nested:
            raise ConfigError(f"cannot set {key} and its [pi] or [sandbox] path")
        if key not in known or isinstance(value, dict):
            raise ConfigError(f"unknown setting: {key}")
        if key in _LEGACY_FLAT_TOML:
            _log.warning(FLAT_TOML_WARNING.format(key=key, path=_LEGACY_FLAT_TOML[key]))
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


def extend_settings(**values: Any) -> Settings:
    try:
        return _ExtendSettings(**values)
    except ValidationError as exc:
        raise ConfigError(_settings_message(exc)) from exc


def _settings_message(exc: ValidationError) -> str:
    for error in exc.errors():
        loc = error.get("loc", ())
        msg = str(error.get("msg", ""))
        if "APIPI_S3_BUCKET" in msg:
            return "APIPI_S3_BUCKET is required"
        if "APIPI_VAULT_MASTER_KEY must be 32 bytes" in msg:
            return "APIPI_VAULT_MASTER_KEY must be 32 bytes (base64 or hex)"
        if "APIPI_RUN_MODE=host is not valid" in msg:
            return "APIPI_RUN_MODE=host is not valid"
        if "APIPI_RUN_MODE=jail is not valid" in msg:
            return "APIPI_RUN_MODE=jail is not valid"
        if RUN_MODE_HELP in msg:
            return RUN_MODE_HELP
        if "database_url" in loc:
            return "DATABASE_URL must be Postgres or SQLite"
        if "run_mode" in loc:
            return RUN_MODE_HELP
        if "env_none_placement" in loc or "APIPI_ENV_NONE_PLACEMENT" in loc:
            return ENV_NONE_PLACEMENT_HELP
        if "idle_ttl" in loc or "APIPI_IDLE_TTL" in loc:
            return "APIPI_IDLE_TTL must be like 15m"
        if (
            "workspace_ttl" in loc
            or "APIPI_WORKSPACE_TTL" in loc
            or "sandbox_ttl_openai_hosted" in loc
            or "APIPI_SANDBOX_TTL_OPENAI_HOSTED" in loc
        ):
            return "APIPI_SANDBOX_TTL_OPENAI_HOSTED must be like 15m or 0"
        if "sandbox_ttl_self_hosted" in loc or "APIPI_SANDBOX_TTL_SELF_HOSTED" in loc:
            return "APIPI_SANDBOX_TTL_SELF_HOSTED must be like 15m or 0"
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
        if "microvm_image" in loc or "APIPI_MICROVM_IMAGE" in loc:
            return MICROVM_IMAGE_HELP
        if "sandbox_default_size" in loc or "APIPI_SANDBOX_DEFAULT_SIZE" in loc:
            return SANDBOX_SIZE_HELP
        if "microvm_mem_mib" in loc:
            return "APIPI_MICROVM_MEM_MIB must be at least 1"
        if "sandbox_m_mem_mib" in loc or "APIPI_SANDBOX_M_MEM_MIB" in loc:
            return "APIPI_SANDBOX_M_MEM_MIB must be at least 1"
        if "sandbox_l_mem_mib" in loc or "APIPI_SANDBOX_L_MEM_MIB" in loc:
            return "APIPI_SANDBOX_L_MEM_MIB must be at least 1"
        if "sandbox_auto_playwright" in loc or "APIPI_SANDBOX_AUTO_PLAYWRIGHT" in loc:
            return "APIPI_SANDBOX_AUTO_PLAYWRIGHT must be on or off"
        if "sandbox_eager_boot" in loc or "APIPI_SANDBOX_EAGER_BOOT" in loc:
            return "APIPI_SANDBOX_EAGER_BOOT must be on or off"
        if "sandbox_l_vcpus" in loc or "APIPI_SANDBOX_L_VCPUS" in loc:
            return "APIPI_SANDBOX_L_VCPUS must be at least 1"
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
    return "invalid configuration"


def require_run_mode(mode: str, settings: Settings | None = None) -> None:
    from apipi.worker.pi.isolation import load_isolation

    load_isolation(mode).require(settings)
