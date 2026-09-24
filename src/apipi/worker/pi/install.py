import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import TextIO

from apipi.config import ConfigError, Settings
from apipi.worker.pi.microvm import (
    default_kernel_path,
    firecracker_bin_dirs,
    kvm_available,
    microvm_image_dir,
    microvm_net_binaries,
)
from apipi.worker.pi.model_host import installed_pi_version
from apipi.worker.pi.version import PI_NPM_PACKAGE, PINNED_FIRECRACKER, PINNED_PI

_FIRECRACKER_ARCH = frozenset({"x86_64", "aarch64"})


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


def firecracker_release_url(arch: str | None = None) -> str:
    machine = arch if arch is not None else os.uname().machine
    if machine not in _FIRECRACKER_ARCH:
        raise ConfigError(f"microvm firecracker has no release for {machine}")
    version = PINNED_FIRECRACKER
    return (
        "https://github.com/firecracker-microvm/firecracker/releases/download/"
        f"v{version}/firecracker-v{version}-{machine}.tgz"
    )


def firecracker_install_dir() -> Path:
    return firecracker_bin_dirs()[0]


def images_root() -> Path:
    here = Path(__file__).resolve().parent
    packaged = here / "images"
    if (packaged / "build.sh").is_file():
        return packaged
    repo = here.parents[3] / "images"
    if (repo / "build.sh").is_file():
        return repo
    raise ConfigError("apipi install --microvm cannot find image recipes")


def read_image_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def recipe_ids() -> list[str]:
    root = images_root()
    found: list[str] = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "image.env").is_file():
            found.append(path.name)
    return found


def require_recipe(image: str) -> Path:
    root = images_root()
    recipe = root / image
    env_path = recipe / "image.env"
    if not env_path.is_file():
        known = ", ".join(recipe_ids()) or "none"
        raise ConfigError(f"unknown microVM image {image}; known recipes: {known}")
    declared = read_image_env(env_path).get("IMAGE_ID", "")
    if declared != image:
        raise ConfigError(f"recipe {image} declares IMAGE_ID {declared or 'unset'}")
    return recipe


def rootfs_output_name(image: str) -> str:
    if image == "default":
        return "rootfs.ext4"
    if image == "browser":
        return "rootfs-browser.ext4"
    return f"rootfs-{image}.ext4"


def rootfs_script_path() -> Path:
    return images_root() / "build.sh"


def rootfs_build_args(image: str, out_dir: Path) -> list[str]:
    require_recipe(image)
    return ["bash", str(rootfs_script_path()), image, str(out_dir)]


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


def _firecracker_version(binary: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = f"{result.stdout} {result.stderr}"
    for part in text.replace(",", " ").split():
        if part.startswith("v") and len(part) > 1 and part[1].isdigit():
            return part[1:]
        if part[:1].isdigit() and "." in part:
            return part
    return None


def _release_bins_ok(dest_dir: Path) -> bool:
    firecracker = dest_dir / "firecracker"
    jailer = dest_dir / "jailer"
    pinned = PINNED_FIRECRACKER
    return (
        firecracker.is_file()
        and jailer.is_file()
        and _firecracker_version(firecracker) == pinned
        and _firecracker_version(jailer) == pinned
    )


def _extract_release_bin(
    tar: tarfile.TarFile,
    prefix: str,
    dest: Path,
    *,
    arch: str | None = None,
) -> None:
    machine = os.uname().machine if arch is None else arch
    expected = f"{prefix}{PINNED_FIRECRACKER}-{machine}"
    for member in tar.getmembers():
        if not member.isfile():
            continue
        name = Path(member.name).name
        if name.endswith(".debug") or name != expected:
            continue
        extracted = tar.extractfile(member)
        if extracted is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with extracted, dest.open("wb") as out:
            shutil.copyfileobj(extracted, out)
        dest.chmod(0o755)
        return
    raise ConfigError(f"Firecracker release tarball has no {expected} binary")


def _download_firecracker(dest_dir: Path, *, stream: TextIO) -> None:
    machine = os.uname().machine
    url = firecracker_release_url(machine)
    req = urllib.request.Request(url, headers={"User-Agent": "apipi"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
    except OSError as exc:
        raise ConfigError(
            f"could not download Firecracker {PINNED_FIRECRACKER}"
        ) from exc
    with tempfile.NamedTemporaryFile(suffix=".tgz") as tmp:
        tmp.write(data)
        tmp.flush()
        with tarfile.open(tmp.name, "r:gz") as tar:
            _extract_release_bin(
                tar, "firecracker-v", dest_dir / "firecracker", arch=machine
            )
            _extract_release_bin(tar, "jailer-v", dest_dir / "jailer", arch=machine)
    print(
        f"Installed Firecracker {PINNED_FIRECRACKER} at {dest_dir / 'firecracker'}",
        file=stream,
    )


def _require_microvm_host() -> None:
    if not kvm_available():
        raise ConfigError("microvm requires /dev/kvm")
    microvm_net_binaries()


def _print_microvm_snippet(image: str, stream: TextIO) -> None:
    kernel = default_kernel_path()
    rootfs = microvm_image_dir() / rootfs_output_name(image)
    print(f"export APIPI_MICROVM_KERNEL={kernel}", file=stream)
    if image == "browser":
        print(f"export APIPI_MICROVM_ROOTFS_BROWSER={rootfs}", file=stream)
        print("export APIPI_MICROVM_IMAGE=browser", file=stream)
    else:
        print(f"export APIPI_MICROVM_ROOTFS={rootfs}", file=stream)
    print(
        "Unset, apipi uses those files when they exist. "
        "apipi microvm shell re-runs under sudo if TAP/jailer need root.",
        file=stream,
    )


def install_microvm(
    *,
    image: str = "default",
    force: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> int:
    stream: TextIO = sys.stdout if out is None else out
    require_recipe(image)
    if not dry_run:
        _require_microvm_host()
    dest_dir = firecracker_install_dir()
    fc = dest_dir / "firecracker"
    jailer = dest_dir / "jailer"
    url = firecracker_release_url()
    out_dir = microvm_image_dir()
    rootfs = out_dir / rootfs_output_name(image)
    kernel = default_kernel_path()
    args = rootfs_build_args(image, out_dir)
    if dry_run:
        print(url, file=stream)
        print(" ".join(args), file=stream)
        return 0
    if _release_bins_ok(dest_dir) and not force:
        print(
            f"Firecracker {PINNED_FIRECRACKER} is already installed at {fc}",
            file=stream,
        )
    else:
        if (fc.is_file() or jailer.is_file()) and not force:
            print(
                "Firecracker or jailer is missing or failed --version; "
                f"reinstalling {PINNED_FIRECRACKER}",
                file=stream,
            )
        _download_firecracker(dest_dir, stream=stream)
        if not _release_bins_ok(dest_dir):
            raise ConfigError(
                "firecracker and jailer --version must both match "
                f"Firecracker {PINNED_FIRECRACKER}"
            )
    if kernel.is_file() and rootfs.is_file() and not force:
        print(f"MicroVM {image} image is already installed", file=stream)
    else:
        env = os.environ.copy()
        env["PINNED_PI"] = PINNED_PI
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(args, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            raise ConfigError("could not build the microVM guest image") from exc
        if not kernel.is_file() or not rootfs.is_file():
            raise ConfigError(f"rootfs build did not write {kernel} and {rootfs}")
    _print_microvm_snippet(image, stream)
    return 0


def _read_choice(
    heading: str,
    options: dict[str, str],
    default: str,
    inp: TextIO,
    out: TextIO,
) -> str:
    print(heading, file=out)
    for key, label in options.items():
        marker = " (default)" if key == default else ""
        print(f"  {key}) {label}{marker}", file=out)
    print(f"Choice [{default}]: ", end="", file=out, flush=True)
    raw = inp.readline()
    if raw == "":
        raise ConfigError("apipi install needs a choice")
    choice = raw.strip() or default
    if choice not in options:
        allowed = ", ".join(options)
        raise ConfigError(f"apipi install choice must be {allowed}")
    return choice


def resolve_install_targets(
    *,
    pi: bool | None,
    microvm: bool | None,
    image: str | None,
    tty: bool,
    inp: TextIO,
    out: TextIO,
) -> tuple[bool, bool, str]:
    flavor = image if image is not None else "default"
    if pi is not None or microvm is not None:
        want_pi = bool(pi)
        want_microvm = bool(microvm)
        if not want_pi and not want_microvm:
            want_microvm = image is not None
        if want_microvm and image is None and tty:
            picked = _read_choice(
                "MicroVM image flavor?",
                {"1": "default", "2": "browser"},
                "1",
                inp,
                out,
            )
            flavor = "browser" if picked == "2" else "default"
        return want_pi, want_microvm, flavor
    if not tty:
        return True, False, flavor
    picked = _read_choice(
        "What should apipi install?",
        {
            "1": "Pi CLI",
            "2": "MicroVM (Firecracker, jailer, guest image)",
            "3": "Both",
        },
        "1",
        inp,
        out,
    )
    want_pi = picked in {"1", "3"}
    want_microvm = picked in {"2", "3"}
    if want_microvm and image is None:
        flavor_pick = _read_choice(
            "MicroVM image flavor?",
            {"1": "default", "2": "browser"},
            "1",
            inp,
            out,
        )
        flavor = "browser" if flavor_pick == "2" else "default"
    return want_pi, want_microvm, flavor


def run_install(
    settings: Settings,
    *,
    pi: bool | None = None,
    microvm: bool | None = None,
    image: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    tty: bool | None = None,
    inp: TextIO | None = None,
    out: TextIO | None = None,
) -> int:
    stream: TextIO = sys.stdout if out is None else out
    in_stream: TextIO = sys.stdin if inp is None else inp
    is_tty = sys.stdin.isatty() if tty is None else tty
    want_pi, want_microvm, flavor = resolve_install_targets(
        pi=pi,
        microvm=microvm,
        image=image,
        tty=is_tty,
        inp=in_stream,
        out=stream,
    )
    if not want_pi and not want_microvm:
        raise ConfigError("apipi install needs --pi and/or --microvm")
    if want_pi:
        install_pi(settings, force=force, dry_run=dry_run, out=stream)
    if want_microvm:
        install_microvm(image=flavor, force=force, dry_run=dry_run, out=stream)
    return 0
