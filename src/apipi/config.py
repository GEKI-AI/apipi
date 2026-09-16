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
MicrovmImage = Literal["default", "browser"]
BUILTIN_RUN_MODES: frozenset[str] = frozenset({"none", "microvm"})
MICROVM_IMAGE_HELP = "APIPI_MICROVM_IMAGE must be default or browser"

NONE_MODE_WARNING = "APIPI_RUN_MODE=none is not suited for production"
SQLITE_WARNING = (
    "SQLite is for one process. Do not share the file across processes or nodes."
)
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
OPENAI_API_KEY_IGNORED = (
    "OPENAI_API_KEY is ignored; the request bearer is sent to the model host"
)
FLAT_TOML_WARNING = "TOML key {key} is deprecated; use {path}"

_log = logging.getLogger("apipi")

_PI_TOML = {
    "command": "pi_command",
    "auto_compact": "pi_auto_compact",
    "platform_prompt": "platform_prompt",
    "platform_prompt_additional": "platform_prompt_additional",
}
_SANDBOX_TOML = {
    "backend": "run_mode",
    "kernel": "microvm_kernel",
    "rootfs": "microvm_rootfs",
    "rootfs_browser": "microvm_rootfs_browser",
    "image": "microvm_image",
}
_SANDBOX_RESOURCES_TOML = {"mem_mib": "microvm_mem_mib", "vcpus": "microvm_vcpus"}
_SANDBOX_NETWORK_TOML = {
    "egress_allowlist": "microvm_egress_allowlist",
    "egress_hosts": "microvm_egress_hosts",
    "egress_mbit": "microvm_egress_mbit",
}
_SANDBOX_TTL_TOML = {
    "openai_hosted": "workspace_ttl",
    "self_hosted": "sandbox_ttl_self_hosted",
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


def parse_microvm_image(value: object) -> object:
    if value is None:
        return "default"
    if isinstance(value, str) and not value.strip():
        return "default"
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
    worker_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_WORKER_TOKEN", "worker_token"),
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
    platform_prompt: str | None = Field(
        default=None,
        validation_alias=AliasChoices("APIPI_PLATFORM_PROMPT", "platform_prompt"),
    )
    platform_prompt_additional: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APIPI_PLATFORM_PROMPT_ADDITIONAL", "platform_prompt_additional"
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
    microvm_image: MicrovmImageName = Field(
        default="default",
        validation_alias=AliasChoices("APIPI_MICROVM_IMAGE", "microvm_image"),
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
    usage_sinks: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_USAGE_SINKS", "usage_sinks"),
    )
    payload_sinks: HostList = Field(
        default="",
        validation_alias=AliasChoices("APIPI_PAYLOAD_SINKS", "payload_sinks"),
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
            self.worker_memory_mb = self.max_sessions * self.microvm_mem_mib
        return self

    def node_memory_mb(self) -> int:
        memory = self.worker_memory_mb
        if memory is None:
            return self.max_sessions * self.microvm_mem_mib
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
    if "pi" in raw:
        nested.update(_map_table(_require_table(raw.pop("pi"), "[pi]"), _PI_TOML, "pi"))
    if "sandbox" in raw:
        nested.update(_flatten_sandbox(_require_table(raw.pop("sandbox"), "[sandbox]")))
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
            return "DATABASE_URL must be Postgres or SQLite"
        if "run_mode" in loc:
            return RUN_MODE_HELP
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
        if "forward_models" in loc or "APIPI_FORWARD_MODELS" in loc:
            return "APIPI_FORWARD_MODELS must be on or off"
        if "pi_auto_compact" in loc or "APIPI_PI_AUTO_COMPACT" in loc:
            return "APIPI_PI_AUTO_COMPACT must be on or off"
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
        if "log_level" in loc:
            return "APIPI_LOG_LEVEL must be debug, info, warning, error, or critical"
        if "log_format" in loc or "APIPI_LOG_FORMAT" in loc:
            return "APIPI_LOG_FORMAT must be json or text"
        if "db_pool_size" in loc:
            return "APIPI_DB_POOL_SIZE must be at least 1"
        if "microvm_image" in loc or "APIPI_MICROVM_IMAGE" in loc:
            return MICROVM_IMAGE_HELP
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


def require_run_mode(mode: str, settings: Settings | None = None) -> None:
    from apipi.pi.isolation import load_isolation

    load_isolation(mode).require(settings)
