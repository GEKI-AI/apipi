from datetime import timedelta
from pathlib import Path

import pytest

from apipi.cli import main
from apipi.config import (
    ConfigError,
    Settings,
    load_settings,
    parse_bytes,
    postgres_url,
)


def test_postgres_url_accepts_postgresql() -> None:
    assert postgres_url("postgresql://apipi:apipi@localhost:5432/apipi") == (
        "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
    )


def test_postgres_url_rejects_non_postgres() -> None:
    with pytest.raises(ConfigError, match="Postgres"):
        postgres_url("sqlite+aiosqlite:///:memory:")


def test_migrate_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["migrate"]) == 1


def test_run_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    assert Settings().run_mode == "host"


def test_idle_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_IDLE_TTL", "15m")
    assert Settings().idle_ttl == timedelta(minutes=15)


def test_workspace_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_WORKSPACE_TTL", "2h")
    assert Settings().workspace_ttl == timedelta(hours=2)


def test_workspace_ttl_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    monkeypatch.setenv("APIPI_WORKSPACE_TTL", "nope")
    with pytest.raises(ConfigError, match="APIPI_WORKSPACE_TTL must be like"):
        load_settings()


def test_default_run_mode_is_jail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    assert Settings().run_mode == "jail"


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
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    monkeypatch.setenv("APIPI_LOG_PROMPTS", "1")
    with pytest.raises(ConfigError, match="prompt or completion bodies"):
        load_settings()


def test_prompt_body_logging_off_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
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
    monkeypatch.delenv("APIPI_TURN_TIMEOUT", raising=False)
    settings = Settings()
    assert settings.max_sessions == 32
    assert settings.max_sessions_per_tenant == 32
    assert settings.turn_timeout == timedelta(minutes=10)
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.log_level == "info"
    assert settings.jail_memory == 512 * 1024 * 1024
    assert settings.max_request_bytes == 1024 * 1024
    assert settings.max_workspace_bytes == 1024 * 1024 * 1024
    assert settings.max_artifact_bytes == 512 * 1024 * 1024
    assert settings.db_pool_size == 5
    assert settings.microvm_mem_mib == 512
    assert settings.microvm_vcpus == 1
    assert settings.microvm_egress_allowlist is True
    assert settings.microvm_egress_hosts == ""
    assert settings.microvm_egress_mbit == 50
    assert settings.workspace_ttl == timedelta(hours=1)
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


def test_egress_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_ALLOWLIST", "off")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_HOSTS", "mcp.tavily.com, api.example.com")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_MBIT", "25")
    settings = Settings()
    assert settings.microvm_egress_allowlist is False
    assert settings.microvm_egress_hosts == "mcp.tavily.com, api.example.com"
    assert settings.microvm_egress_mbit == 25


def test_egress_mbit_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    monkeypatch.setenv("APIPI_MICROVM_EGRESS_MBIT", "0")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_EGRESS_MBIT must be"):
        load_settings()


def test_max_sessions_per_tenant_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    monkeypatch.setenv("APIPI_MAX_SESSIONS_PER_TENANT", "0")
    with pytest.raises(ConfigError, match="APIPI_MAX_SESSIONS_PER_TENANT must be"):
        load_settings()


def test_load_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "host"\n'
        "max_sessions = 4\n"
        'idle_ttl = "5m"\n'
    )
    settings = load_settings()
    assert settings.run_mode == "host"
    assert settings.max_sessions == 4
    assert settings.idle_ttl == timedelta(minutes=5)


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "jail"\n'
    )
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    assert load_settings().run_mode == "host"


def test_dotenv_overrides_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        'run_mode = "jail"\n'
    )
    (tmp_path / ".env").write_text("APIPI_RUN_MODE=host\n")
    assert load_settings().run_mode == "host"


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
