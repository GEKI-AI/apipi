from pathlib import Path

import pytest

from apipi.cli import main
from apipi.config import ConfigError, Settings
from apipi.pi.microvm import SHELL_WARNING


class _Tty:
    def isatty(self) -> bool:
        return True


class _Pipe:
    def isatty(self) -> bool:
        return False


def test_cli_microvm_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        main(["microvm"])


def test_cli_microvm_shell_needs_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("apipi.cli.sys.stdin", _Pipe())
    assert main(["microvm", "shell"]) == 1
    assert "needs a TTY" in capsys.readouterr().err


def test_cli_microvm_shell_image_and_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    captured: dict[str, object] = {}

    async def fake_run(settings: Settings, *, cwd: str | None = None) -> int:
        captured["image"] = settings.microvm_image
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr("apipi.cli.sys.stdin", _Tty())
    monkeypatch.setattr("apipi.cli.run_microvm_shell", fake_run)
    monkeypatch.setattr(
        "apipi.cli.load_settings",
        lambda config_path=None: Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            microvm_image="default",
        ),
    )
    assert (
        main(["microvm", "shell", "--image", "browser", "--workspace", str(workspace)])
        == 0
    )
    assert captured["image"] == "browser"
    assert captured["cwd"] == str(workspace)
    assert SHELL_WARNING in capsys.readouterr().err


def test_cli_microvm_shell_prints_tap_rights(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom(_settings: Settings, *, cwd: str | None = None) -> int:
        raise ConfigError(
            "APIPI_RUN_MODE=microvm cannot create a TAP device: "
            "Operation not permitted. Need root or CAP_NET_ADMIN "
            "(and CAP_NET_RAW) for TAP, NAT, and ip_forward."
        )

    monkeypatch.setattr("apipi.cli.sys.stdin", _Tty())
    monkeypatch.setattr("apipi.cli.run_microvm_shell", boom)
    monkeypatch.setattr(
        "apipi.cli.load_settings",
        lambda config_path=None: Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
        ),
    )
    assert main(["microvm", "shell"]) == 1
    err = capsys.readouterr().err
    assert "TAP device" in err
    assert "CAP_NET_ADMIN" in err
    assert "Operation not permitted" in err
