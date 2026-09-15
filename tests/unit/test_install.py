from io import StringIO
from pathlib import Path

import pytest

from apipi.cli import main
from apipi.config import ConfigError, Settings
from apipi.pi.install import (
    firecracker_release_url,
    install_microvm,
    install_pi,
    npm_install_args,
    pi_install_prefix,
    resolve_install_targets,
    run_install,
)
from apipi.pi.version import PI_NPM_PACKAGE, PINNED_FIRECRACKER, PINNED_PI


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


def test_firecracker_release_url_pins_arch() -> None:
    url = firecracker_release_url("x86_64")
    assert PINNED_FIRECRACKER in url
    assert url.endswith(f"firecracker-v{PINNED_FIRECRACKER}-x86_64.tgz")
    with pytest.raises(ConfigError, match="no release"):
        firecracker_release_url("ppc64")


def test_resolve_install_targets_non_tty() -> None:
    out = StringIO()
    assert resolve_install_targets(
        pi=None, microvm=None, image=None, tty=False, inp=StringIO(""), out=out
    ) == (True, False, "default")


def test_resolve_install_targets_prompt() -> None:
    out = StringIO()
    assert resolve_install_targets(
        pi=None,
        microvm=None,
        image=None,
        tty=True,
        inp=StringIO("3\n2\n"),
        out=out,
    ) == (True, True, "browser")
    text = out.getvalue()
    assert "What should apipi install?" in text
    assert "MicroVM image flavor?" in text


def test_resolve_install_targets_flags_skip_prompt() -> None:
    out = StringIO()
    assert resolve_install_targets(
        pi=False,
        microvm=True,
        image="browser",
        tty=True,
        inp=StringIO("should-not-read\n"),
        out=out,
    ) == (False, True, "browser")
    assert out.getvalue() == ""


def test_install_microvm_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out = StringIO()
    assert install_microvm(dry_run=True, out=out) == 0
    text = out.getvalue()
    assert PINNED_FIRECRACKER in text
    assert "firecracker-v" in text
    assert "--flavor default" in text


def test_install_microvm_skips_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("apipi.pi.install.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.install.microvm_net_binaries", lambda: ("ip", "iptables", "tc")
    )
    dest = tmp_path / "apipi" / "firecracker"
    dest.mkdir(parents=True)
    (dest / "firecracker").write_text("")
    (dest / "jailer").write_text("")
    cache = tmp_path / "cache" / "apipi" / "microvm"
    cache.mkdir(parents=True)
    (cache / "vmlinux").write_bytes(b"k")
    (cache / "rootfs.ext4").write_bytes(b"r")
    monkeypatch.setattr(
        "apipi.pi.install._firecracker_version", lambda _path: PINNED_FIRECRACKER
    )
    ran: list[object] = []
    monkeypatch.setattr(
        "apipi.pi.install.subprocess.run", lambda *_a, **_k: ran.append(1)
    )
    monkeypatch.setattr(
        "apipi.pi.install._download_firecracker", lambda *_a, **_k: ran.append("dl")
    )
    out = StringIO()
    assert install_microvm(out=out) == 0
    assert ran == []
    text = out.getvalue()
    assert "already installed" in text
    assert "APIPI_MICROVM_KERNEL" in text
    assert "APIPI_MICROVM_ROOTFS=" in text


def test_run_install_microvm_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out = StringIO()
    assert (
        run_install(
            _settings(),
            pi=False,
            microvm=True,
            dry_run=True,
            tty=False,
            out=out,
        )
        == 0
    )
    assert PINNED_FIRECRACKER in out.getvalue()


def test_cli_install_microvm_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert main(["install", "--microvm", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert PINNED_FIRECRACKER in out
    assert "npm install" not in out


def test_cli_install_role_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    assert main(["install", "--role", "api"]) == 0
    assert "API role needs no Pi or MicroVM install" in capsys.readouterr().out
