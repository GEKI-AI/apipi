import logging
from pathlib import Path

import pytest

from apipi.cli import main, prepare_serve
from apipi.config import (
    METRICS_OFF,
    METRICS_ON,
    NONE_MODE_WARNING,
    OTEL_SET,
    OTEL_UNSET,
    PAYLOAD_EXPORT_OFF,
    SQLITE_WARNING,
    USAGE_EXPORT_OFF,
    USAGE_STORE_TURNS,
    ConfigError,
    Settings,
    require_run_mode,
)


def _none_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


def _noop_probe(_settings: Settings) -> None:
    return None


@pytest.fixture(autouse=True)
def _skip_model_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.cli.probe_model_host", _noop_probe)
    monkeypatch.setattr("apipi.cli.upgrade_head", lambda _url: None)


def test_probe_run_mode_skips_none() -> None:
    from apipi.pi.probe import probe_run_mode

    probe_run_mode(_none_settings())


def test_prepare_serve_warns_on_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    prepare_serve(_none_settings())
    assert NONE_MODE_WARNING in caplog.text


def test_prepare_serve_logs_default_observability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="apipi")
    prepare_serve(_none_settings())
    messages = [record.getMessage() for record in caplog.records]
    assert USAGE_STORE_TURNS in messages
    assert "usage retention 15d" in messages
    assert USAGE_EXPORT_OFF in messages
    assert PAYLOAD_EXPORT_OFF in messages
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
        run_mode="none",
        metrics=True,
        otel_endpoint="http://otel:4318",
    )
    prepare_serve(settings)
    messages = [record.getMessage() for record in caplog.records]
    assert USAGE_STORE_TURNS in messages
    assert METRICS_ON in messages
    assert OTEL_SET in messages
    assert METRICS_OFF not in messages
    assert OTEL_UNSET not in messages


def test_prepare_serve_ignores_legacy_turn_log_flag(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("APIPI_TURN_LOG", "off")
    caplog.set_level(logging.INFO, logger="apipi")
    prepare_serve(_none_settings())
    assert USAGE_STORE_TURNS in caplog.text


def test_prepare_serve_rejects_prompt_body_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APIPI_LOG_PROMPTS", "1")
    with pytest.raises(ConfigError, match="prompt or completion bodies"):
        prepare_serve(_none_settings())


def test_prepare_serve_warns_on_sqlite(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        run_mode="none",
    )
    caplog.set_level(logging.WARNING, logger="apipi")
    prepare_serve(settings)
    assert SQLITE_WARNING in caplog.text


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
    monkeypatch.setattr("apipi.cli.probe_run_mode", _noop_probe)
    caplog.set_level(logging.WARNING)
    called: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_kwargs: object) -> None:
        called["host"] = host
        called["port"] = port
        called["app"] = app

    monkeypatch.setattr("apipi.cli.uvicorn.run", fake_run)
    assert main(["serve"]) == 0
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8000
    assert NONE_MODE_WARNING not in caplog.text


def test_serve_host_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "host")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_jail_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "jail")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_microvm_probe_fail_does_not_listen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    def fail(_settings: Settings) -> None:
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not start")

    monkeypatch.setattr("apipi.cli.probe_run_mode", fail)
    monkeypatch.setattr("apipi.cli.uvicorn.run", boom)
    assert main(["serve"]) == 1


def test_serve_none_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    caplog.set_level(logging.WARNING)
    called: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_kwargs: object) -> None:
        called["host"] = host
        called["port"] = port
        called["app"] = app

    monkeypatch.setattr("apipi.cli.uvicorn.run", fake_run)
    assert main(["serve"]) == 0
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8000
    assert NONE_MODE_WARNING in caplog.text


def test_serve_defaults_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    caplog.set_level(logging.WARNING, logger="apipi")
    monkeypatch.setattr("apipi.cli.uvicorn.run", lambda *_a, **_k: None)
    assert main(["serve"]) == 0
    assert SQLITE_WARNING in caplog.text
