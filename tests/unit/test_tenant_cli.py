import pytest

from apipi.cli import main


def test_tenant_create_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["tenant", "create", "--name", "local"]) == 1
