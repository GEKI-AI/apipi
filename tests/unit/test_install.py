from io import StringIO
from pathlib import Path

import pytest

from apipi.cli import main
from apipi.config import ConfigError, Settings
from apipi.pi.install import install_pi, npm_install_args, pi_install_prefix
from apipi.pi.version import PI_NPM_PACKAGE, PINNED_PI


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


def test_npm_install_args_pin_package() -> None:
    prefix = Path("/tmp/apipi-pi")
    args = npm_install_args(prefix, force=False)
    assert args[:3] == ["npm", "install", "--ignore-scripts"]
    assert f"{PI_NPM_PACKAGE}@{PINNED_PI}" in args
    assert "--force" not in args
    assert "--force" in npm_install_args(prefix, force=True)


def test_install_dry_run_prints_npm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("apipi.pi.install.installed_pi_version", lambda _s: None)
    monkeypatch.setattr("apipi.pi.install.shutil.which", lambda _name: "/usr/bin/npm")
    out = StringIO()
    assert install_pi(_settings(), dry_run=True, out=out) == 0
    text = out.getvalue()
    assert "npm install" in text
    assert PINNED_PI in text
    assert str(pi_install_prefix()) in text


def test_install_skips_when_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.pi.install.installed_pi_version", lambda _s: PINNED_PI)
    ran: list[object] = []
    monkeypatch.setattr(
        "apipi.pi.install.subprocess.run", lambda *_a, **_k: ran.append(1)
    )
    out = StringIO()
    assert install_pi(_settings(), out=out) == 0
    assert ran == []
    assert f"Pi {PINNED_PI} is already installed" in out.getvalue()


def test_install_force_reruns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("apipi.pi.install.installed_pi_version", lambda _s: PINNED_PI)
    monkeypatch.setattr("apipi.pi.install.shutil.which", lambda _name: "/usr/bin/npm")

    def fake_run(args: list[str], check: bool) -> None:
        del check
        binary = tmp_path / "apipi" / "pi" / "node_modules" / ".bin" / "pi"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        assert "--force" in args

    monkeypatch.setattr("apipi.pi.install.subprocess.run", fake_run)
    out = StringIO()
    assert install_pi(_settings(), force=True, out=out) == 0
    assert f"Installed Pi {PINNED_PI}" in out.getvalue()
    assert "APIPI_PI_COMMAND" in out.getvalue()


def test_install_requires_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.pi.install.installed_pi_version", lambda _s: None)
    monkeypatch.setattr("apipi.pi.install.shutil.which", lambda _name: None)
    with pytest.raises(ConfigError, match="npm is not on PATH"):
        install_pi(_settings())


def test_cli_install_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setattr("apipi.pi.install.installed_pi_version", lambda _s: None)
    monkeypatch.setattr("apipi.pi.install.shutil.which", lambda _name: "/usr/bin/npm")
    assert main(["install", "--dry-run"]) == 0
    assert "npm install" in capsys.readouterr().out
