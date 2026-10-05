import os
import subprocess
import tarfile
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from tests.support.config import none_settings

from apipi.cli import main
from apipi.common.image_recipes import read_image_env, recipe_ids
from apipi.config import ConfigError
from apipi.worker.pi import install as pi_install
from apipi.worker.pi.install import (
    _extract_release_bin,
    _firecracker_version,
    firecracker_release_url,
    install_microvm,
    install_pi,
    npm_install_args,
    pi_install_prefix,
    require_recipe,
    resolve_install_targets,
    rootfs_build_args,
    rootfs_script_path,
    run_install,
)
from apipi.worker.pi.version import PI_NPM_PACKAGE, PINNED_FIRECRACKER, PINNED_PI


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
    monkeypatch.setattr(pi_install, "installed_pi_version", lambda _s: None)
    monkeypatch.setattr(pi_install.shutil, "which", lambda _name: "/usr/bin/npm")
    out = StringIO()
    assert install_pi(none_settings(), dry_run=True, out=out) == 0
    text = out.getvalue()
    assert "npm install" in text
    assert PINNED_PI in text
    assert str(pi_install_prefix()) in text


def test_install_skips_when_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_install, "installed_pi_version", lambda _s: PINNED_PI)
    ran: list[object] = []
    monkeypatch.setattr(
        "apipi.worker.pi.install.subprocess.run", lambda *_a, **_k: ran.append(1)
    )
    out = StringIO()
    assert install_pi(none_settings(), out=out) == 0
    assert ran == []
    assert f"Pi {PINNED_PI} is already installed" in out.getvalue()


def test_install_force_reruns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(pi_install, "installed_pi_version", lambda _s: PINNED_PI)
    monkeypatch.setattr(pi_install.shutil, "which", lambda _name: "/usr/bin/npm")

    def fake_run(args: list[str], check: bool) -> None:
        del check
        binary = tmp_path / "apipi" / "pi" / "node_modules" / ".bin" / "pi"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        assert "--force" in args

    monkeypatch.setattr("apipi.worker.pi.install.subprocess.run", fake_run)
    out = StringIO()
    assert install_pi(none_settings(), force=True, out=out) == 0
    assert f"Installed Pi {PINNED_PI}" in out.getvalue()
    assert "APIPI_PI_COMMAND" in out.getvalue()


def test_install_requires_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi_install, "installed_pi_version", lambda _s: None)
    monkeypatch.setattr(pi_install.shutil, "which", lambda _name: None)
    with pytest.raises(ConfigError, match="npm is not on PATH"):
        install_pi(none_settings())


def test_cli_install_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setattr(pi_install, "installed_pi_version", lambda _s: None)
    monkeypatch.setattr(pi_install.shutil, "which", lambda _name: "/usr/bin/npm")
    assert main(["install", "--dry-run"]) == 0
    assert "npm install" in capsys.readouterr().out


def test_kernel_url_pins_firecracker_ci() -> None:
    version, build = pi_install.pinned_kernel()
    url = pi_install.kernel_url("x86_64")
    assert version == "6.1.186"
    assert build in url
    assert url.endswith(f"x86_64/vmlinux-{version}")
    assert "quickstart_guide" not in url


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
    assert "apipi images pull default" in text


def test_install_microvm_pulls_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("apipi.worker.pi.install.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.install.microvm_net_binaries", lambda: ("ip", "iptables", "tc")
    )
    dest = tmp_path / "apipi" / "firecracker"
    dest.mkdir(parents=True)
    (dest / "firecracker").write_text("")
    (dest / "jailer").write_text("")
    monkeypatch.setattr(
        "apipi.worker.pi.install._firecracker_version", lambda _path: PINNED_FIRECRACKER
    )
    monkeypatch.setattr(
        "apipi.worker.pi.install.subprocess.run", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        pi_install,
        "_download_firecracker",
        lambda *_a, **_k: None,
    )
    pulled: list[list[str] | None] = []

    def fake_pull(
        settings: object, *, ids: list[str] | None = None, **_k: object
    ) -> list[str]:
        pulled.append(ids)
        return [f"{ids[0]} v1" if ids else "default v1"]

    monkeypatch.setattr("apipi.worker.pi.image_pull.pull_images", fake_pull)
    monkeypatch.setattr(
        "apipi.worker.pi.image_pull.configured_images_dir",
        lambda _s: tmp_path / "images",
    )
    out = StringIO()
    assert install_microvm(out=out) == 0
    assert pulled == [["default"]]
    text = out.getvalue()
    assert "Pulled MicroVM image default" in text


def _tar_member(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o755
    tar.addfile(info, BytesIO(payload))


def test_extract_release_bin_skips_debug_first(tmp_path: Path) -> None:
    machine = "x86_64"
    prefix = "jailer-v"
    release = f"{prefix}{PINNED_FIRECRACKER}-{machine}"
    tar_path = tmp_path / "release.tar"
    dest = tmp_path / "jailer"
    with tarfile.open(tar_path, "w") as tar:
        _tar_member(tar, f"dir/{release}.debug", b"debug-jailer")
        _tar_member(tar, f"dir/{release}", b"release-jailer")
    with tarfile.open(tar_path, "r") as tar:
        _extract_release_bin(tar, prefix, dest, arch=machine)
    assert dest.read_bytes() == b"release-jailer"
    assert dest.stat().st_mode & 0o111


def test_extract_release_bin_rejects_debug_only(tmp_path: Path) -> None:
    machine = "x86_64"
    prefix = "jailer-v"
    release = f"{prefix}{PINNED_FIRECRACKER}-{machine}"
    tar_path = tmp_path / "debug-only.tar"
    with tarfile.open(tar_path, "w") as tar:
        _tar_member(tar, f"dir/{release}.debug", b"debug-jailer")
    with tarfile.open(tar_path, "r") as tar, pytest.raises(ConfigError, match=release):
        _extract_release_bin(tar, prefix, tmp_path / "jailer", arch=machine)


def test_firecracker_version_rejects_crash(tmp_path: Path) -> None:
    binary = tmp_path / "jailer"
    binary.write_text("#!/bin/sh\necho 'Jailer v1.17.0'\nexit 139\n")
    binary.chmod(0o755)
    assert _firecracker_version(binary) is None


def test_firecracker_version_reads_jailer(tmp_path: Path) -> None:
    binary = tmp_path / "jailer"
    binary.write_text(f"#!/bin/sh\necho 'Jailer v{PINNED_FIRECRACKER}'\n")
    binary.chmod(0o755)
    assert _firecracker_version(binary) == PINNED_FIRECRACKER


def test_install_microvm_repairs_bad_jailer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("apipi.worker.pi.install.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.install.microvm_net_binaries", lambda: ("ip", "iptables", "tc")
    )
    dest = tmp_path / "apipi" / "firecracker"
    dest.mkdir(parents=True)
    (dest / "firecracker").write_text("")
    (dest / "jailer").write_text("")
    state: dict[str, str | None] = {"jailer": None}

    def version(path: Path) -> str | None:
        if Path(path).name == "jailer":
            return state["jailer"]
        return PINNED_FIRECRACKER

    downloaded: list[Path] = []

    def fake_dl(dest_dir: Path, *, stream: object) -> None:
        del stream
        state["jailer"] = PINNED_FIRECRACKER
        downloaded.append(dest_dir)

    monkeypatch.setattr("apipi.worker.pi.install._firecracker_version", version)
    monkeypatch.setattr(pi_install, "_download_firecracker", fake_dl)
    monkeypatch.setattr(
        "apipi.worker.pi.image_pull.pull_images", lambda *_a, **_k: ["default v1"]
    )
    monkeypatch.setattr(
        "apipi.worker.pi.image_pull.configured_images_dir",
        lambda _s: tmp_path / "images",
    )
    ran: list[object] = []
    monkeypatch.setattr(
        "apipi.worker.pi.install.subprocess.run", lambda *_a, **_k: ran.append(1)
    )
    out = StringIO()
    assert install_microvm(out=out) == 0
    assert downloaded == [dest]
    assert ran == []
    text = out.getvalue()
    assert "reinstalling" in text
    assert f"Firecracker {PINNED_FIRECRACKER} is already installed" not in text
    assert "Pulled MicroVM image default" in text


def test_install_microvm_errors_when_jailer_still_bad(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("apipi.worker.pi.install.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.install.microvm_net_binaries", lambda: ("ip", "iptables", "tc")
    )
    dest = tmp_path / "apipi" / "firecracker"
    dest.mkdir(parents=True)
    (dest / "firecracker").write_text("")
    (dest / "jailer").write_text("")

    def version(path: Path) -> str | None:
        if Path(path).name == "jailer":
            return None
        return PINNED_FIRECRACKER

    monkeypatch.setattr("apipi.worker.pi.install._firecracker_version", version)
    monkeypatch.setattr(pi_install, "_download_firecracker", lambda *_a, **_k: None)
    with pytest.raises(ConfigError, match="jailer --version"):
        install_microvm(out=StringIO())


def test_run_install_microvm_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out = StringIO()
    assert (
        run_install(
            none_settings(),
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


def test_recipe_ids_list_shipped_images() -> None:
    assert recipe_ids() == ["browser", "default", "work"]
    default = read_image_env(require_recipe("default") / "image.env")
    browser = read_image_env(require_recipe("browser") / "image.env")
    assert default["SIZE_MIB"] == "2048"
    assert default["PACKAGES"] == ""
    assert default["MIN_SIZE"] == "S"
    assert browser["SIZE_MIB"] == "4096"
    assert "fonts-noto-cjk" in browser["PACKAGES"].split()
    assert browser["ARCHS"] == "x86_64"
    assert browser["MIN_VCPUS"] == "2"
    assert browser["MIN_SIZE"] == "M"
    work = read_image_env(require_recipe("work") / "image.env")
    assert work["IMAGE_ID"] == "work"
    assert work["SIZE_MIB"] == "3072"
    assert work["MIN_SIZE"] == "M"
    assert "python3-pandas" in work["PACKAGES"].split()
    setup = (require_recipe("work") / "setup.sh").read_text()
    assert "python-docx==1.2.0" in setup
    assert "python-pptx==1.0.2" in setup
    assert "fpdf2==2.8.9" in setup
    assert "pdftotext" in setup
    assert rootfs_script_path().name == "build.sh"


def test_build_script_points_to_images_build() -> None:
    script = rootfs_script_path().read_text()
    assert "apipi images build" in script
    assert "APIPI_MICROVM_ROOTFS_BROWSER" not in script
    assert "APIPI_MICROVM_IMAGE=browser" not in script
    assert "dev-only overrides" in script


def test_build_script_base_packages_include_curl_and_git() -> None:
    script = rootfs_script_path().read_text()
    assert "apt-get install" in script
    assert "python3-pip" in script
    assert "ripgrep" in script
    assert "curl" in script
    assert "git" in script
    assert "ALPINE_VER" not in script
    assert "apk add" not in script
    assert "PINNED_DEBIAN_DIGEST" in script
    assert "PINNED_NODE" in script
    assert "PINNED_UV" in script
    assert "/etc/pip.conf" in script
    assert "UV_CACHE_DIR" not in script


def test_rootfs_build_args_for_shipped_images(tmp_path: Path) -> None:
    default_args = rootfs_build_args("default", tmp_path)
    browser_args = rootfs_build_args("browser", tmp_path)
    assert default_args[0] == "bash"
    assert default_args[1].endswith("images/build.sh")
    assert default_args[2:] == ["default", str(tmp_path)]
    assert browser_args[2:] == ["browser", str(tmp_path)]


def test_unknown_image_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown microVM image nope"):
        rootfs_build_args("nope", tmp_path)
    with pytest.raises(ConfigError, match="known recipes"):
        install_microvm(image="nope", dry_run=True, out=StringIO())


def test_rootfs_wrapper_resolves_checkout_paths(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nexit 1\n")
    curl.chmod(0o755)
    env = os.environ.copy()
    env.pop("PINNED_PI", None)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts" / "microvm-rootfs"),
            "--flavor",
            "default",
            str(tmp_path / "out"),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "src/apipi/pi/" not in result.stderr
    assert "could not read PINNED_PI" not in result.stderr
    assert result.returncode != 0


def test_cli_install_role_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    assert main(["install", "--role", "api"]) == 0
    assert "API role needs no Pi or MicroVM install" in capsys.readouterr().out
