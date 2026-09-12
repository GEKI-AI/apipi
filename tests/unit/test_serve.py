import logging
from pathlib import Path

import pytest

from apipi.cli import main, prepare_serve
from apipi.config import (
    HOST_MODE_WARNING,
    METRICS_OFF,
    METRICS_ON,
    OTEL_SET,
    OTEL_UNSET,
    TURN_LOG_ON,
    ConfigError,
    Settings,
    require_run_mode,
)


def _host_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
    )


def test_prepare_serve_warns_on_host(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    prepare_serve(_host_settings())
    assert HOST_MODE_WARNING in caplog.text


def test_prepare_serve_logs_default_observability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="apipi")
    prepare_serve(_host_settings())
    messages = [record.getMessage() for record in caplog.records]
    assert TURN_LOG_ON in messages
    assert METRICS_OFF in messages
    assert OTEL_UNSET in messages
    assert METRICS_ON not in messages
    assert OTEL_SET not in messages


def test_prepare_serve_logs_enabled_exports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="apipi")
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
        metrics=True,
        otel_endpoint="http://otel:4318",
    )
    prepare_serve(settings)
    messages = [record.getMessage() for record in caplog.records]
    assert TURN_LOG_ON in messages
    assert METRICS_ON in messages
    assert OTEL_SET in messages
    assert METRICS_OFF not in messages
    assert OTEL_UNSET not in messages


def test_prepare_serve_turn_log_stays_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("APIPI_TURN_LOG", "off")
    caplog.set_level(logging.INFO, logger="apipi")
    prepare_serve(_host_settings())
    assert TURN_LOG_ON in caplog.text


def test_prepare_serve_rejects_prompt_body_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIPI_LOG_PROMPTS", "1")
    with pytest.raises(ConfigError, match="prompt or completion bodies"):
        prepare_serve(_host_settings())


def test_prepare_serve_rejects_sqlite() -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        run_mode="host",
    )
    with pytest.raises(ConfigError, match="Postgres"):
        prepare_serve(settings)


def test_microvm_run_mode_exits_without_kvm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: False)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_run_mode("microvm")


def test_serve_microvm_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_microvm_starts_when_tools_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.write_bytes(b"k")
    rootfs.write_bytes(b"r")
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    monkeypatch.setenv("APIPI_MICROVM_KERNEL", str(kernel))
    monkeypatch.setenv("APIPI_MICROVM_ROOTFS", str(rootfs))
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which", lambda name: f"/usr/bin/{name}"
    )
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
    assert HOST_MODE_WARNING not in caplog.text


def test_serve_jail_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "jail")
    monkeypatch.setattr("apipi.pi.jail.shutil.which", lambda _name: None)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_jail_starts_when_tools_present(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "jail")
    monkeypatch.setattr("apipi.pi.jail.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: True)
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
    assert HOST_MODE_WARNING not in caplog.text


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
