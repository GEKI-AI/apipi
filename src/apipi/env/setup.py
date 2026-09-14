import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


SETUP_SCRIPT = ".apipi/setup.sh"
SETUP_DONE = ".apipi/setup.done"
EGRESS_HOSTS_FILE = ".apipi/egress-hosts"

PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")
NPM_HOSTS = ("registry.npmjs.org", "registry.npmjs.com")
ALPINE_HOSTS = ("dl-cdn.alpinelinux.org",)

_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+=~:-]*$")
_WORKSPACE_PREFIXES = ("/workspace", "/tmp/workspace")


class SetupError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Packages:
    python: tuple[str, ...] = ()
    system: tuple[str, ...] = ()
    npm: tuple[str, ...] = ()

    def empty(self) -> bool:
        return not self.python and not self.system and not self.npm


@dataclass(frozen=True)
class SetupCommand:
    command: str
    cwd: str | None = None


def _names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SetupError("packages values must be lists of names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SetupError("package names must be non-empty strings")
        name = item.strip()
        if _PKG.fullmatch(name) is None:
            raise SetupError(f"invalid package name: {name}")
        names.append(name)
    return tuple(names)


def packages_from(environment: dict[str, Any]) -> Packages:
    raw = environment.get("packages")
    if raw is None:
        return Packages()
    if not isinstance(raw, dict):
        raise SetupError("packages must be an object")
    known = {"python", "system", "npm"}
    extra = set(raw) - known
    if extra:
        raise SetupError("unknown packages key")
    return Packages(
        python=_names(raw.get("python")),
        system=_names(raw.get("system")),
        npm=_names(raw.get("npm")),
    )


def setup_commands_from(environment: dict[str, Any]) -> tuple[SetupCommand, ...]:
    raw = environment.get("setup_commands")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SetupError("setup_commands must be a list")
    commands: list[SetupCommand] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SetupError("setup_commands entries must be objects")
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise SetupError("setup_commands need a command")
        cwd = item.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise SetupError("setup cwd must be a string")
        extra = set(item) - {"command", "cwd"}
        if extra:
            raise SetupError("unknown setup_commands field")
        commands.append(SetupCommand(command=command, cwd=cwd))
    return tuple(commands)


def needs_setup(environment: dict[str, Any]) -> bool:
    if environment.get("type") != "openai_hosted":
        return False
    return not packages_from(environment).empty() or bool(
        setup_commands_from(environment)
    )


def package_egress_hosts(environment: dict[str, Any]) -> tuple[str, ...]:
    packages = packages_from(environment)
    hosts: list[str] = []
    seen: set[str] = set()
    groups: list[tuple[str, ...]] = []
    if packages.python:
        groups.append(PYPI_HOSTS)
    if packages.npm:
        groups.append(NPM_HOSTS)
    if packages.system:
        groups.append(ALPINE_HOSTS)
    for group in groups:
        for host in group:
            key = host.lower()
            if key in seen:
                continue
            seen.add(key)
            hosts.append(host)
    return tuple(hosts)


def workspace_egress_hosts(cwd: str | None) -> list[str]:
    if not cwd:
        return []
    path = Path(cwd) / EGRESS_HOSTS_FILE
    if not path.is_file():
        return []
    hosts: list[str] = []
    for line in path.read_text().splitlines():
        host = line.strip()
        if host:
            hosts.append(host)
    return hosts


def resolve_setup_cwd(workspace: Path, cwd: str | None) -> Path:
    root = workspace.resolve()
    if cwd is None or not cwd.strip() or cwd.strip() in {".", "./"}:
        return root
    raw = cwd.strip()
    for prefix in _WORKSPACE_PREFIXES:
        if raw == prefix:
            return root
        if raw.startswith(prefix + "/"):
            raw = raw[len(prefix) + 1 :]
            break
    else:
        if raw.startswith("/"):
            raise SetupError("setup cwd must be inside the workspace")
    path = (root / raw).resolve()
    if not _is_under(path, root) and path != root:
        raise SetupError("setup cwd must be inside the workspace")
    return path


def render_setup_script(
    workspace: Path, packages: Packages, commands: tuple[SetupCommand, ...]
) -> str:
    lines = [
        "#!/bin/sh",
        "set -e",
        "ROOT=$(pwd)",
    ]
    if packages.python:
        quoted = " ".join(shlex.quote(name) for name in packages.python)
        lines.extend(
            [
                "if command -v uv >/dev/null 2>&1; then",
                f"  uv pip install --python python3 {quoted}",
                "elif python3 -m pip --version >/dev/null 2>&1; then",
                f"  python3 -m pip install {quoted}",
                "elif command -v apk >/dev/null 2>&1; then",
                "  apk add --no-cache py3-pip",
                f"  python3 -m pip install {quoted}",
                "else",
                '  echo "python package install needs uv or pip" >&2',
                "  exit 1",
                "fi",
            ]
        )
    if packages.system:
        quoted = " ".join(shlex.quote(name) for name in packages.system)
        lines.extend(
            [
                "if command -v apk >/dev/null 2>&1; then",
                f"  apk add --no-cache {quoted}",
                "elif command -v apt-get >/dev/null 2>&1; then",
                f"  apt-get install -y {quoted}",
                "else",
                '  echo "system package install needs apk or apt-get" >&2',
                "  exit 1",
                "fi",
            ]
        )
    if packages.npm:
        quoted = " ".join(shlex.quote(name) for name in packages.npm)
        lines.extend(
            [
                "if ! command -v npm >/dev/null 2>&1; then",
                '  echo "npm package install needs npm" >&2',
                "  exit 1",
                "fi",
                f"npm install -g {quoted}",
            ]
        )
    for item in commands:
        dest = resolve_setup_cwd(workspace, item.cwd)
        rel = dest.relative_to(workspace.resolve()).as_posix()
        target = "$ROOT" if rel in {".", ""} else f"$ROOT/{shlex.quote(rel)}"
        lines.extend(
            [
                f"mkdir -p {target}",
                f"( cd {target} && sh -c {shlex.quote(item.command)} )",
            ]
        )
    return "\n".join(lines) + "\n"


def prepare_workspace(workspace: Path, environment: dict[str, Any]) -> None:
    if environment.get("type") != "openai_hosted":
        return
    packages = packages_from(environment)
    commands = setup_commands_from(environment)
    if packages.empty() and not commands:
        return
    workspace.mkdir(parents=True, exist_ok=True)
    apipi = workspace / ".apipi"
    apipi.mkdir(parents=True, exist_ok=True)
    script = render_setup_script(workspace, packages, commands)
    path = workspace / SETUP_SCRIPT
    path.write_text(script)
    path.chmod(0o755)
    hosts = package_egress_hosts(environment)
    egress = workspace / EGRESS_HOSTS_FILE
    if hosts:
        egress.write_text("\n".join(hosts) + "\n")
    elif egress.exists():
        egress.unlink()


def run_host_setup(workspace: Path) -> None:
    script = workspace / SETUP_SCRIPT
    if not script.is_file():
        return
    done = workspace / SETUP_DONE
    if done.is_file():
        return
    try:
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SetupError("environment setup failed") from exc
    log = workspace / ".apipi" / "setup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "environment setup failed").strip()
        raise SetupError(detail or "environment setup failed")
    done.write_text("ok\n")


def provision_hosted(environment: dict[str, Any], *, run_mode: str) -> None:
    if environment.get("type") != "openai_hosted":
        return
    directory = environment.get("directory")
    if not isinstance(directory, str) or directory == "":
        return
    workspace = Path(directory)
    prepare_workspace(workspace, environment)
    if run_mode == "none":
        run_host_setup(workspace)
