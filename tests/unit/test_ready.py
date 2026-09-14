from io import StringIO
from pathlib import Path

import pytest

from apipi import __version__
from apipi.cli import main
from apipi.config import ConfigError, Settings
from apipi.pi.version import PINNED_PI
from apipi.ready import check_ready, run_checks


def _settings(
    *,
    run_mode: str = "none",
    model_base_url: str | None = "http://model.test/v1",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        model_base_url=model_base_url,
    )


def test_run_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.ready.require_pinned_pi", lambda _s: None)
    monkeypatch.setattr("apipi.ready.installed_pi_version", lambda _s: PINNED_PI)
    monkeypatch.setattr("apipi.ready.ping_postgres", lambda _url: None)
    monkeypatch.setattr("apipi.ready.fetch_model_ids", lambda *_a, **_k: ["m1"])
    rows = run_checks(_settings())
    by_name = {row.name: row for row in rows}
    assert by_name["apipi"].status == "ok"
    assert by_name["apipi"].detail == __version__
    assert by_name["pi"].detail == PINNED_PI
    assert by_name["database"].status == "ok"
    assert by_name["model host"].status == "ok"
    assert "not for production" in by_name["run mode"].detail
    assert by_name["auth"].status == "skip"
    assert all(row.status != "fail" for row in rows)


def test_run_checks_pi_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_settings: Settings) -> None:
        raise ConfigError("pi is not on PATH")

    monkeypatch.setattr("apipi.ready.require_pinned_pi", boom)
    monkeypatch.setattr("apipi.ready.ping_postgres", lambda _url: None)
    monkeypatch.setattr("apipi.ready.fetch_model_ids", lambda *_a, **_k: [])
    rows = run_checks(_settings())
    pi = next(row for row in rows if row.name == "pi")
    assert pi.status == "fail"
    assert pi.detail == "pi is not on PATH"


def test_run_checks_skip_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.ready.require_pinned_pi", lambda _s: None)
    monkeypatch.setattr("apipi.ready.installed_pi_version", lambda _s: PINNED_PI)
    rows = run_checks(_settings(), skip_db=True, skip_model=True)
    by_name = {row.name: row for row in rows}
    assert by_name["database"].status == "skip"
    assert by_name["model host"].status == "skip"


def test_run_checks_fast_skips_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = False

    def fake_probe(_settings: Settings) -> None:
        nonlocal probed
        probed = True

    monkeypatch.setattr("apipi.ready.require_pinned_pi", lambda _s: None)
    monkeypatch.setattr("apipi.ready.installed_pi_version", lambda _s: PINNED_PI)
    monkeypatch.setattr("apipi.ready.ping_postgres", lambda _url: None)
    monkeypatch.setattr("apipi.ready.fetch_model_ids", lambda *_a, **_k: [])
    monkeypatch.setattr("apipi.ready.probe_run_mode", fake_probe)
    monkeypatch.setattr("apipi.ready.require_run_mode", lambda *_a, **_k: None)
    settings = _settings(run_mode="tests.support.fake_isolation:FakeIsolation")
    rows = run_checks(settings, fast=True)
    assert probed is False
    skip = next(row for row in rows if row.name == "sandbox probe")
    assert skip.status == "skip"


def test_check_ready_invalid_config(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("not = [toml")
    out = StringIO()
    assert check_ready(config_path=str(path), out=out) == 1
    text = out.getvalue()
    assert text.startswith("fail")
    assert "config" in text


def test_cli_check_skip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setattr("apipi.ready.require_pinned_pi", lambda _s: None)
    monkeypatch.setattr("apipi.ready.installed_pi_version", lambda _s: PINNED_PI)
    assert main(["check", "--skip-db", "--skip-model"]) == 0
    text = capsys.readouterr().out
    assert "apipi" in text
    assert "skip" in text
    assert "database" in text
    assert "model host" in text
