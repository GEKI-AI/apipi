import logging

import pytest

from apipi.cli import main, prepare_serve
from apipi.config import HOST_MODE_WARNING, ConfigError, Settings, require_run_mode


def _host_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
    )


def test_prepare_serve_warns_on_host(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    prepare_serve(_host_settings())
    assert HOST_MODE_WARNING in caplog.text


def test_prepare_serve_rejects_sqlite() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        run_mode="host",
    )
    with pytest.raises(ConfigError, match="Postgres"):
        prepare_serve(settings)


@pytest.mark.parametrize("mode", ["jail", "microvm"])
def test_unimplemented_run_mode_exits(mode: str) -> None:
    with pytest.raises(ConfigError, match="not available"):
        require_run_mode(mode)


def test_serve_jail_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "jail")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_host_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    caplog.set_level(logging.WARNING)
    called: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        called["host"] = host
        called["port"] = port
        called["app"] = app

    monkeypatch.setattr("apipi.cli.uvicorn.run", fake_run)
    assert main(["serve"]) == 0
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8000
    assert HOST_MODE_WARNING in caplog.text


def test_serve_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APIPI_RUN_MODE", "host")
    assert main(["serve"]) == 1
