from datetime import timedelta
from pathlib import Path

import pytest

from apipi.cli import main
from apipi.config import (
    FLAT_TOML_WARNING,
    ConfigError,
    Settings,
    default_sqlite_url,
    extend_settings,
    load_settings,
    parse_bytes,
    postgres_url,
    store_url,
)
from apipi.worker.pi.proc import pi_command_args


def test_postgres_url_accepts_postgresql() -> None:
    assert postgres_url("postgresql://apipi:apipi@localhost:5432/apipi") == (
        "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
    )


def test_postgres_url_rejects_non_postgres() -> None:
    with pytest.raises(ConfigError, match="Postgres or SQLite"):
        postgres_url("sqlite+aiosqlite:///:memory:")


def test_store_url_accepts_sqlite() -> None:
    assert store_url("sqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
    assert store_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_unset_database_url_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.database_url == default_sqlite_url()
    assert settings.database_url.endswith("/.apipi/apipi.db")


def test_extend_settings_ignores_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://hijack:hijack@localhost:5432/hijack"
    )
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    monkeypatch.chdir(tmp_path)
    settings = extend_settings(
        database_url="sqlite+aiosqlite:///:memory:",
        run_mode="none",
    )
    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.run_mode == "none"


def test_extend_settings_defaults_ignore_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://hijack:hijack@localhost:5432/hijack"
    )
    monkeypatch.chdir(tmp_path)
    settings = extend_settings(run_mode="none")
    assert "hijack" not in settings.database_url
    assert settings.database_url == default_sqlite_url()


def test_migrate_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["migrate"]) == 0
    assert (tmp_path / ".apipi" / "apipi.db").is_file()


def test_model_api_key_overwrite_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("OPENAI_API_KEY_OVERWRITE", "operator-key")
    monkeypatch.setenv("OPENAI_API_KEY", "ignored")
    settings = Settings()
    assert settings.model_api_key_overwrite == "operator-key"
    assert not hasattr(settings, "model_api_key")


def test_run_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    assert Settings().run_mode == "none"


def test_idle_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_IDLE_TTL", "15m")
    assert Settings().idle_ttl == timedelta(minutes=15)


def test_worker_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_WORKER_TOKEN", "secret")
    monkeypatch.setenv("APIPI_WORKER_LEASE_TTL", "15s")
    settings = Settings()
    assert settings.worker_token == "secret"
    assert settings.worker_lease_ttl == timedelta(seconds=15)


def test_vault_master_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    raw = base64.b64encode(bytes(range(32))).decode()
    monkeypatch.setenv("APIPI_VAULT_MASTER_KEY", raw)
    assert Settings().vault_master_key == raw


def test_vault_master_key_rejects_short(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APIPI_VAULT_MASTER_KEY", "nope")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="32 bytes"):
        load_settings()


def test_worker_memory_mb_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_WORKER_MEMORY_MB", "57344")
    settings = Settings()
    assert settings.worker_memory_mb == 57344
    assert settings.node_memory_mb() == 57344


def test_sandbox_default_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_WORKER_MEMORY_MB", raising=False)
    monkeypatch.delenv("APIPI_MAX_SESSIONS", raising=False)
    monkeypatch.setenv("APIPI_SANDBOX_DEFAULT_SIZE", "L")
    settings = Settings()
    assert settings.sandbox_default_size == "L"
    assert settings.node_memory_mb() == 32 * 2048


def test_sandbox_default_size_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_SANDBOX_DEFAULT_SIZE", "XL")
    with pytest.raises(ConfigError, match="APIPI_SANDBOX_DEFAULT_SIZE must be"):
        load_settings()


def test_worker_memory_mb_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_WORKER_MEMORY_MB", "0")
    with pytest.raises(ConfigError, match="APIPI_WORKER_MEMORY_MB must be"):
        load_settings()


def test_pi_mem_mib_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_PI_MEM_MIB", "256")
    assert Settings().pi_mem_mib == 256


def test_pi_mem_mib_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_PI_MEM_MIB", "0")
    with pytest.raises(ConfigError, match="APIPI_PI_MEM_MIB must be"):
        load_settings()


def test_workspace_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_WORKSPACE_TTL", "2h")
    assert Settings().workspace_ttl == timedelta(hours=2)


def test_sandbox_ttl_openai_hosted_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_WORKSPACE_TTL", raising=False)
    monkeypatch.setenv("APIPI_SANDBOX_TTL_OPENAI_HOSTED", "45m")
    settings = Settings()
    assert settings.workspace_ttl == timedelta(minutes=45)
    assert settings.sandbox_ttl_for("openai_hosted") == timedelta(minutes=45)


def test_sandbox_ttl_zero_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_WORKSPACE_TTL", raising=False)
    monkeypatch.setenv("APIPI_SANDBOX_TTL_OPENAI_HOSTED", "0")
    monkeypatch.setenv("APIPI_SANDBOX_TTL_SELF_HOSTED", "0")
    settings = Settings()
    assert settings.workspace_ttl is None
    assert settings.sandbox_ttl_self_hosted is None
    assert settings.sandbox_ttl_for("openai_hosted") is None
    assert settings.pi_idle_ttl_for("openai_hosted") is None
    assert settings.pi_idle_ttl_for("none") == timedelta(minutes=15)


def test_workspace_ttl_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_WORKSPACE_TTL", "nope")
    with pytest.raises(
        ConfigError, match="APIPI_SANDBOX_TTL_OPENAI_HOSTED must be like"
    ):
        load_settings()


def test_default_run_mode_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    assert Settings().run_mode == "none"


def test_run_mode_host_is_not_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=host is not valid"):
        load_settings()


def test_run_mode_jail_is_not_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "jail")
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=jail is not valid"):
        load_settings()


def test_run_mode_custom_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "tests.support.fake_isolation:FakeIsolation")
    assert Settings().run_mode == "tests.support.fake_isolation:FakeIsolation"


def test_run_mode_chat_is_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "chat")
    assert Settings().run_mode == "chat"


def test_run_mode_unknown_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "gvisor")
    with pytest.raises(
        ConfigError, match=r"none, chat, microvm, or package\.mod:Class"
    ):
        load_settings()


def test_default_auth_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_AUTH", raising=False)
    monkeypatch.delenv("APIPI_AUTH_CACHE_TTL", raising=False)
    assert Settings().auth is None
    assert Settings().auth_cache_ttl == timedelta(seconds=30)


def test_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_AUTH", "pkg.mod:func")
    monkeypatch.setenv("APIPI_AUTH_CACHE_TTL", "45s")
    assert Settings().auth == "pkg.mod:func"
    assert Settings().auth_cache_ttl == timedelta(seconds=45)


def test_default_exports_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_METRICS", raising=False)
    monkeypatch.delenv("APIPI_OTEL_ENDPOINT", raising=False)
    settings = Settings()
    assert settings.metrics is False
    assert settings.otel_endpoint is None
    assert "turn_log" not in type(settings).model_fields
    assert "log_prompts" not in type(settings).model_fields


def test_metrics_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_METRICS", "on")
    assert Settings().metrics is True
    monkeypatch.setenv("APIPI_METRICS", "off")
    assert Settings().metrics is False


def test_forward_models_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_FORWARD_MODELS", raising=False)
    assert Settings().forward_models is True


def test_forward_models_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_FORWARD_MODELS", "off")
    assert Settings().forward_models is False
    monkeypatch.setenv("APIPI_FORWARD_MODELS", "on")
    assert Settings().forward_models is True


def test_microvm_image_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_MICROVM_IMAGE", raising=False)
    assert Settings().microvm_image == "default"
    assert Settings().microvm_rootfs_browser is None


def test_microvm_image_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_MICROVM_IMAGE", "browser")
    monkeypatch.setenv("APIPI_MICROVM_ROOTFS_BROWSER", "/tmp/rootfs-browser.ext4")
    settings = Settings()
    assert settings.microvm_image == "browser"
    assert settings.microvm_rootfs_browser == "/tmp/rootfs-browser.ext4"


def test_microvm_image_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_MICROVM_IMAGE", "gpu")
    with pytest.raises(
        ConfigError, match="APIPI_MICROVM_IMAGE must be default or browser"
    ):
        load_settings()


def test_otel_endpoint_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_OTEL_ENDPOINT", "http://otel:4318")
    assert Settings().otel_endpoint == "http://otel:4318"
    monkeypatch.setenv("APIPI_OTEL_ENDPOINT", "")
    assert Settings().otel_endpoint is None


def test_prompt_body_logging_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_LOG_PROMPTS", "1")
    with pytest.raises(ConfigError, match="prompt or completion bodies"):
        load_settings()


def test_prompt_body_logging_off_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_LOG_PROMPTS", "off")
    settings = load_settings()
    assert settings.metrics is False
    assert settings.otel_endpoint is None


def test_parse_bytes() -> None:
    assert parse_bytes("512M") == 512 * 1024 * 1024
    assert parse_bytes("1MiB") == 1024 * 1024
    assert parse_bytes("1024") == 1024


def test_new_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_MAX_SESSIONS", raising=False)
    monkeypatch.delenv("APIPI_WORKER_MEMORY_MB", raising=False)
    monkeypatch.delenv("APIPI_SANDBOX_DEFAULT_SIZE", raising=False)
    monkeypatch.delenv("APIPI_TURN_TIMEOUT", raising=False)
    settings = Settings()
    assert settings.max_sessions == 32
    assert settings.max_sessions_per_tenant == 32
    assert settings.turn_timeout == timedelta(minutes=10)
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.instance_id is None
    assert settings.log_level == "info"
    assert settings.log_format == "json"
    assert settings.max_request_bytes == 1024 * 1024
    assert settings.max_workspace_bytes == 1024 * 1024 * 1024
    assert settings.max_artifact_bytes == 512 * 1024 * 1024
    assert settings.max_file_bytes == 50 * 1024 * 1024
    assert settings.max_file_bytes == 50 * 1024 * 1024
    assert settings.artifact_store == "local"
    assert settings.s3_bucket is None
    assert settings.s3_region == "us-east-1"
    assert settings.s3_prefix == "apipi/artifacts"
    assert settings.s3_addressing == "auto"
    assert settings.db_pool_size == 5
    assert settings.worker_memory_mb == 16384
    assert settings.node_memory_mb() == 16384
    assert settings.sandbox_default_size == "S"
    assert settings.sandbox_auto_playwright is True
    assert settings.sandbox_playwright_mcp == "@playwright/mcp@latest"
    assert settings.microvm_mem_mib == 512
    assert settings.sandbox_m_mem_mib == 1024
    assert settings.sandbox_l_mem_mib == 2048
    assert settings.microvm_vcpus == 1
    assert settings.microvm_egress_allowlist is False
    assert settings.microvm_egress_hosts == ""
    assert settings.microvm_egress_mbit == 50
    assert settings.workspace_ttl == timedelta(hours=1)
    assert settings.usage_store == "turns"
    assert settings.env_none_placement == "chat"
    assert settings.usage_retention == timedelta(days=15)
    assert settings.usage_export_url is None
    assert settings.usage_export_token is None
    assert settings.usage_export_timeout == timedelta(seconds=5)
    assert settings.usage_export_retries == 1
    assert settings.payload_export_url is None
    assert settings.payload_export_token is None
    assert settings.payload_export_timeout == timedelta(seconds=5)
    assert settings.payload_export_retries == 1
    assert settings.usage_sinks == ""
    assert settings.payload_sinks == ""
    assert settings.pi_auto_compact is True
    assert "example_ui" not in type(settings).model_fields


def test_limit_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_MAX_SESSIONS_PER_TENANT", "4")
    monkeypatch.setenv("APIPI_MAX_WORKSPACE_BYTES", "1GiB")
    monkeypatch.setenv("APIPI_MAX_ARTIFACT_BYTES", "512MiB")
    settings = Settings()
    assert settings.max_sessions_per_tenant == 4
    assert settings.max_workspace_bytes == 1024 * 1024 * 1024
    assert settings.max_artifact_bytes == 512 * 1024 * 1024
    assert settings.max_file_bytes == 50 * 1024 * 1024


def test_instance_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_INSTANCE_ID", "node-a")
    assert Settings().instance_id == "node-a"
    monkeypatch.setenv("APIPI_INSTANCE_ID", "")
    assert Settings().instance_id is None


def test_instance_id_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_INSTANCE_ID", "bad\nid")
    with pytest.raises(ConfigError, match="APIPI_INSTANCE_ID must be short ASCII"):
        load_settings()


def test_egress_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_ALLOWLIST", "on")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_HOSTS", "mcp.tavily.com, api.example.com")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_MBIT", "25")
    settings = Settings()
    assert settings.microvm_egress_allowlist is True
    assert settings.microvm_egress_hosts == "mcp.tavily.com, api.example.com"
    assert settings.microvm_egress_mbit == 25


def test_egress_mbit_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_MBIT", "0")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_EGRESS_MBIT must be"):
        load_settings()


def test_max_sessions_per_tenant_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_MAX_SESSIONS_PER_TENANT", "0")
    with pytest.raises(ConfigError, match="APIPI_MAX_SESSIONS_PER_TENANT must be"):
        load_settings()


def test_load_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "none"\n'
        "max_sessions = 4\n"
        'idle_ttl = "5m"\n'
    )
    settings = load_settings()
    assert settings.run_mode == "none"
    assert settings.max_sessions == 4
    assert settings.worker_memory_mb == 2048
    assert settings.idle_ttl == timedelta(minutes=5)


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "microvm"\n'
    )
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    assert load_settings().run_mode == "none"


def test_dotenv_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "microvm"\n'
    )
    (tmp_path / ".env").write_text("APIPI_RUN_MODE=none\n")
    assert load_settings().run_mode == "none"


def test_unknown_toml_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\nworkers = 4\n'
    )
    with pytest.raises(ConfigError, match="unknown setting: workers"):
        load_settings()


def test_config_path_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(config_path=str(tmp_path / "missing.toml"))


def test_usage_store_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_USAGE_STORE", "rollups")
    monkeypatch.setenv("APIPI_USAGE_RETENTION", "90d")
    monkeypatch.setenv("APIPI_USAGE_EXPORT_URL", "https://example.test/usage")
    monkeypatch.setenv("APIPI_USAGE_EXPORT_RETRIES", "0")
    settings = Settings()
    assert settings.usage_store == "rollups"
    assert settings.usage_retention == timedelta(days=90)
    assert settings.usage_export_url == "https://example.test/usage"
    assert settings.usage_export_retries == 0


def test_usage_retention_empty_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_USAGE_RETENTION", "")
    assert load_settings().usage_retention is None


def test_log_format_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_LOG_FORMAT", "yaml")
    with pytest.raises(ConfigError, match="APIPI_LOG_FORMAT must be"):
        load_settings()


def test_usage_store_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_USAGE_STORE", "warehouse")
    with pytest.raises(ConfigError, match="APIPI_USAGE_STORE must be"):
        load_settings()


def test_usage_export_url_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_USAGE_EXPORT_URL", "not-a-url")
    with pytest.raises(ConfigError, match="APIPI_USAGE_EXPORT_URL must be"):
        load_settings()


def test_payload_export_url_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_PAYLOAD_EXPORT_URL", "not-a-url")
    with pytest.raises(ConfigError, match="APIPI_PAYLOAD_EXPORT_URL must be"):
        load_settings()


def test_explicit_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "custom.toml"
    path.write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'log_level = "debug"\n'
    )
    settings = load_settings(config_path=str(path))
    assert settings.log_level == "debug"


def test_nested_toml_sandbox_and_pi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "max_sessions = 8\n"
        "[pi]\n"
        'command = "pi-dev"\n'
        "auto_compact = false\n"
        "mem_mib = 384\n"
        "[sandbox]\n"
        'backend = "microvm"\n'
        'kernel = "/tmp/vmlinux"\n'
        'rootfs = "/tmp/rootfs.ext4"\n'
        'rootfs_browser = "/tmp/rootfs-browser.ext4"\n'
        'image = "browser"\n'
        'default_size = "M"\n'
        "[sandbox.resources]\n"
        "mem_mib = 1024\n"
        "m_mem_mib = 1536\n"
        "l_mem_mib = 3072\n"
        "vcpus = 2\n"
        "[sandbox.network]\n"
        "egress_allowlist = false\n"
        'egress_hosts = "mcp.example.com"\n'
        "egress_mbit = 25\n"
        "[sandbox.ttl]\n"
        'openai_hosted = "45m"\n'
        'self_hosted = "0"\n'
        "[sandbox.browser]\n"
        "auto_playwright = false\n"
        'playwright_mcp = "@playwright/mcp@1.2.3"\n'
    )
    settings = load_settings()
    assert settings.max_sessions == 8
    assert settings.pi_command == "pi-dev"
    assert settings.pi_auto_compact is False
    assert settings.pi_mem_mib == 384
    assert settings.platform_prompt is None
    assert settings.platform_prompt_additional == ""
    assert settings.run_mode == "microvm"
    assert settings.microvm_kernel == "/tmp/vmlinux"
    assert settings.microvm_rootfs == "/tmp/rootfs.ext4"
    assert settings.microvm_rootfs_browser == "/tmp/rootfs-browser.ext4"
    assert settings.microvm_image == "browser"
    assert settings.sandbox_default_size == "M"
    assert settings.microvm_mem_mib == 1024
    assert settings.sandbox_m_mem_mib == 1536
    assert settings.sandbox_l_mem_mib == 3072
    assert settings.microvm_vcpus == 2
    assert settings.microvm_egress_allowlist is False
    assert settings.microvm_egress_hosts == "mcp.example.com"
    assert settings.microvm_egress_mbit == 25
    assert settings.workspace_ttl == timedelta(minutes=45)
    assert settings.sandbox_ttl_self_hosted is None
    assert settings.sandbox_auto_playwright is False
    assert settings.sandbox_playwright_mcp == "@playwright/mcp@1.2.3"


def test_nested_toml_platform_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_PLATFORM_PROMPT", raising=False)
    monkeypatch.delenv("APIPI_PLATFORM_PROMPT_ADDITIONAL", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "none"\n'
        "[pi]\n"
        'platform_prompt = ""\n'
        'platform_prompt_additional = "Always answer in German."\n'
    )
    settings = load_settings()
    assert settings.platform_prompt == ""
    assert settings.platform_prompt_additional == "Always answer in German."


def test_legacy_flat_toml_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "none"\n'
    )
    caplog.set_level("WARNING", logger="apipi")
    settings = load_settings()
    assert settings.run_mode == "none"
    assert FLAT_TOML_WARNING.format(key="run_mode", path="[sandbox].backend") in (
        caplog.text
    )


def test_nested_and_flat_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "none"\n'
        "[sandbox]\n"
        'backend = "microvm"\n'
    )
    with pytest.raises(ConfigError, match="cannot set run_mode"):
        load_settings()


def test_unknown_nested_toml_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "[sandbox]\n"
        "jail = true\n"
    )
    with pytest.raises(ConfigError, match=r"unknown setting: sandbox\.jail"):
        load_settings()


def test_env_overrides_nested_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "[sandbox]\n"
        'backend = "microvm"\n'
    )
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    assert load_settings().run_mode == "none"


def test_pi_auto_compact_false_adds_flag() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        pi_auto_compact=False,
    )
    args = pi_command_args(settings, tools=False)
    assert "--no-auto-compact" in args


def test_pi_auto_compact_default_omits_flag() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    args = pi_command_args(settings, tools=False)
    assert "--no-auto-compact" not in args


def test_env_none_placement_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_ENV_NONE_PLACEMENT", "microvm")
    assert load_settings().env_none_placement == "microvm"


def test_env_none_placement_from_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_ENV_NONE_PLACEMENT", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "[placement]\n"
        'env_none = "reject"\n'
    )
    assert load_settings().env_none_placement == "reject"


def test_pi_thinking_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_PI_THINKING", "medium")
    assert load_settings().pi_thinking == "medium"


def test_pi_thinking_from_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APIPI_PI_THINKING", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "[pi]\n"
        'thinking = "high"\n'
    )
    assert load_settings().pi_thinking == "high"


def test_pi_thinking_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_PI_THINKING", "maxed")
    with pytest.raises(ConfigError, match="APIPI_PI_THINKING must be"):
        load_settings()


def test_env_none_placement_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_ENV_NONE_PLACEMENT", "host")
    with pytest.raises(ConfigError, match="APIPI_ENV_NONE_PLACEMENT must be"):
        load_settings()
