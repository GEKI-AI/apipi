import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from apipi.config import ConfigError, Settings
from apipi.pi.model_host import installed_pi_version
from apipi.pi.version import PI_NPM_PACKAGE, PINNED_PI


def pi_install_prefix() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    base = Path(raw) if raw else Path.home() / ".local" / "share"
    return base / "apipi" / "pi"


def pi_install_bin(prefix: Path | None = None) -> Path:
    root = prefix if prefix is not None else pi_install_prefix()
    return root / "node_modules" / ".bin" / "pi"


def npm_install_args(prefix: Path, *, force: bool) -> list[str]:
    spec = f"{PI_NPM_PACKAGE}@{PINNED_PI}"
    args = ["npm", "install", "--ignore-scripts", "--prefix", str(prefix), spec]
    if force:
        args.append("--force")
    return args


def install_pi(
    settings: Settings,
    *,
    force: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    stream: TextIO = sys.stdout if out is None else out
    current = installed_pi_version(settings)
    if current == PINNED_PI and not force:
        print(f"Pi {PINNED_PI} is already installed", file=stream)
        return 0
    npm = shutil.which("npm")
    if npm is None:
        raise ConfigError(
            "npm is not on PATH; install Node.js or use the npm one-liner "
            "in the install docs"
        )
    prefix = pi_install_prefix()
    args = npm_install_args(prefix, force=force)
    if dry_run:
        print(" ".join(args), file=stream)
        return 0
    prefix.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"npm could not install Pi {PINNED_PI}") from exc
    binary = pi_install_bin(prefix)
    if not binary.exists():
        raise ConfigError(f"npm install did not write {binary}")
    print(f"Installed Pi {PINNED_PI} at {binary}", file=stream)
    print(
        f"Put {binary.parent} on PATH, or set APIPI_PI_COMMAND={binary}",
        file=stream,
    )
    return 0
