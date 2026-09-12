import pytest

from apipi.cli import main


def test_cli_has_no_tenant_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        main(["tenant", "create", "--name", "local"])
