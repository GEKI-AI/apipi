import pytest

from apipi.cli import main
from apipi.config import ConfigError, postgres_url


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
