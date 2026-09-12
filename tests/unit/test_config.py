from datetime import timedelta

import pytest

from apipi.cli import main
from apipi.config import ConfigError, Settings, postgres_url


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
