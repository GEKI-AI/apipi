import logging
from pathlib import Path
from typing import Any

import pytest

from apipi.cli import main, prepare_serve, prepare_worker
from apipi.config import (
    API_ONLY_REMOVED,
    LIFECYCLE_EXPORT_OFF,
    METRICS_OFF,
    METRICS_ON,
    OTEL_SET,
    OTEL_UNSET,
    PAYLOAD_EXPORT_OFF,
    SQLITE_WARNING,
    USAGE_EXPORT_OFF,
    USAGE_STORE_TURNS,
    VAULT_MASTER_KEY_UNSET,
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


def _worker_settings(tmp_path: Path, **kwargs: Any) -> Settings:
    token_file = tmp_path / "worker.token"
    token_file.write_text("test-token\n")
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        worker_token_file=str(token_file),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _skip_model_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.cli.probe_model_host", _noop_probe)


def test_probe_run_mode_skips_none() -> None:
    from apipi.worker.pi.probe import probe_run_mode

    probe_run_mode(_none_settings())


def test_prepare_worker_none_skips_production_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probed: list[str] = []

    def fake_probe(settings: Settings) -> None:
        probed.append(settings.run_mode)

    monkeypatch.setattr("apipi.cli.probe_run_mode", fake_probe)
    caplog.set_level(logging.INFO, logger="apipi")
    settings = prepare_worker(_worker_settings(tmp_path, run_mode="none"))
    assert settings.run_mode == "none"
    assert probed == ["none"]
    assert "not suited for production" not in caplog.text
    # Workers never decrypt vaults, so the vault key warning is API-only.
    assert VAULT_MASTER_KEY_UNSET not in caplog.text


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
    assert LIFECYCLE_EXPORT_OFF in messages
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
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: False)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_run_mode("microvm")


def test_worker_requires_token_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("APIPI_WORKER_TOKEN_FILE", raising=False)
    assert main(["worker"]) == 1


def test_worker_microvm_exits_without_kvm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    monkeypatch.setenv("APIPI_WORKER_TOKEN_FILE", str(tmp_path / "worker.token"))
    (tmp_path / "worker.token").write_text("test-token\n")
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: False)

    async def boom(_settings: Settings, *, url: str | None = None) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr("apipi.worker.hub.run_worker", boom)
    assert main(["worker"]) == 1


def test_prepare_worker_probes_model_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Settings] = []
    monkeypatch.setattr("apipi.cli.probe_model_host", lambda item: seen.append(item))
    monkeypatch.setattr("apipi.cli.probe_run_mode", lambda _item: None)
    prepare_worker(
        _worker_settings(
            tmp_path, run_mode="none", model_base_url="http://model.test/v1"
        )
    )
    assert seen
    assert seen[0].model_base_url == "http://model.test/v1"


def test_prepare_worker_probes_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probed: list[str] = []

    def fake_probe(settings: Settings) -> None:
        probed.append(settings.run_mode)

    monkeypatch.setattr("apipi.cli.probe_run_mode", fake_probe)
    settings = prepare_worker(_worker_settings(tmp_path, run_mode="none"))
    assert settings.run_mode == "none"
    assert probed == ["none"]


def test_serve_never_probes_the_run_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("serve must not probe the sandbox")

    monkeypatch.setattr("apipi.worker.pi.probe.probe_run_mode", boom)
    called: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int, **_kwargs: object) -> None:
        called["host"] = host
        called["app"] = app

    monkeypatch.setattr("apipi.cli.uvicorn.run", fake_run)
    assert main(["serve"]) == 0
    assert called["host"] == "0.0.0.0"


def test_serve_rejects_the_removed_api_only_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["serve", "--api-only"])
    assert raised.value.code == 2
    assert "--api-only" in capsys.readouterr().err


def test_serve_warns_on_removed_api_only_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_API_ONLY", "1")
    caplog.set_level(logging.WARNING, logger="apipi")
    called: list[object] = []
    monkeypatch.setattr("apipi.cli.uvicorn.run", lambda app, **_k: called.append(app))
    assert main(["serve"]) == 0
    assert called
    records = [r for r in caplog.records if r.getMessage() == API_ONLY_REMOVED]
    assert len(records) == 1
    assert getattr(records[0], "event", None) == "config.api_only_removed"


def test_serve_warns_on_removed_api_only_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("APIPI_API_ONLY", raising=False)
    config = tmp_path / "apipi.toml"
    config.write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "api_only = true\n"
    )
    caplog.set_level(logging.WARNING, logger="apipi")
    called: list[object] = []
    monkeypatch.setattr("apipi.cli.uvicorn.run", lambda app, **_k: called.append(app))
    assert main(["serve", "--config", str(config)]) == 0
    assert called
    assert API_ONLY_REMOVED in caplog.text


def test_worker_warns_on_removed_api_only_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APIPI_API_ONLY", "1")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    token = tmp_path / "worker.token"
    token.write_text("test-token\n")
    monkeypatch.setenv("APIPI_WORKER_TOKEN_FILE", str(token))
    monkeypatch.setattr("apipi.cli.probe_run_mode", _noop_probe)
    caplog.set_level(logging.WARNING, logger="apipi")

    async def fake_run(_settings: Settings, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr("apipi.worker.hub.run_worker", fake_run)
    assert main(["worker"]) == 0
    assert API_ONLY_REMOVED in caplog.text


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
