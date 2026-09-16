import base64
import os
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
USER_ENV_FILE = ".apipi/user.env"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "PWD",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DATABASE_URL",
        "PI_CODING_AGENT_DIR",
    }
)
_RESERVED_ENV_PREFIXES = ("APIPI_", "CODEX_", "PI_")

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


def _reserved_env_name(name: str) -> bool:
    if name in _RESERVED_ENV:
        return True
    return any(name.startswith(prefix) for prefix in _RESERVED_ENV_PREFIXES)


def session_env_from(environment: dict[str, Any]) -> dict[str, str]:
    raw = environment.get("env")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SetupError("env must be an object")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None:
            raise SetupError("env names must be identifiers")
        if _reserved_env_name(key):
            raise SetupError(f"reserved env name: {key}")
        if not isinstance(value, str):
            raise SetupError("env values must be strings")
        values[key] = value
    return values


def inline_files_from(environment: dict[str, Any]) -> list[tuple[str, bytes]]:
    raw = environment.get("files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SetupError("files must be a list")
    files: list[tuple[str, bytes]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SetupError("files entries must be objects")
        if item.get("type") != "inline":
            raise SetupError("files entries must have type inline")
        path = item.get("path")
        data = item.get("data")
        if not isinstance(path, str) or not path.strip():
            raise SetupError("files need a path")
        if not isinstance(data, str):
            raise SetupError("files data must be a base64 string")
        try:
            decoded = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise SetupError("files data must be base64") from exc
        files.append((path.strip(), decoded))
    return files


def needs_setup(environment: dict[str, Any]) -> bool:
    if environment.get("type") != "openai_hosted":
        return False
    return (
        not packages_from(environment).empty()
        or bool(setup_commands_from(environment))
        or bool(session_env_from(environment))
        or bool(inline_files_from(environment))
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


def resolve_workspace_path(workspace: Path, raw: str, *, kind: str) -> Path:
    root = workspace.resolve()
    text = raw.strip()
    if not text:
        raise SetupError(f"{kind} path is required")
    for prefix in _WORKSPACE_PREFIXES:
        if text == prefix:
            path = root
            break
        if text.startswith(prefix + "/"):
            text = text[len(prefix) + 1 :]
            path = (root / text).resolve()
            break
    else:
        if text.startswith("/"):
            raise SetupError(f"{kind} path must be inside the workspace")
        path = (root / text).resolve()
    if not _is_under(path, root) and path != root:
        raise SetupError(f"{kind} path must be inside the workspace")
    return path


def resolve_setup_cwd(workspace: Path, cwd: str | None) -> Path:
    if cwd is None or not cwd.strip() or cwd.strip() in {".", "./"}:
        return workspace.resolve()
    return resolve_workspace_path(workspace, cwd, kind="setup cwd")


def render_setup_script(
    workspace: Path, packages: Packages, commands: tuple[SetupCommand, ...]
) -> str:
    lines = [
        "#!/bin/sh",
        "set -e",
        "ROOT=$(pwd)",
        f'if [ -f "$ROOT/{USER_ENV_FILE}" ]; then',
        "  set -a",
        f'  . "$ROOT/{USER_ENV_FILE}"',
        "  set +a",
        "fi",
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


def write_user_env(workspace: Path, values: dict[str, str]) -> None:
    path = workspace / USER_ENV_FILE
    if not values:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={shlex.quote(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")


def write_inline_files(
    workspace: Path,
    files: list[tuple[str, bytes]],
    *,
    max_bytes: int | None = None,
) -> None:
    root = workspace.resolve()
    apipi = (root / ".apipi").resolve()
    total = 0
    for raw_path, data in files:
        total += len(data)
        if max_bytes is not None and total > max_bytes:
            raise SetupError("inline files exceed workspace size")
        dest = resolve_workspace_path(workspace, raw_path, kind="file")
        if dest == root:
            raise SetupError("file path must be a file inside the workspace")
        if dest == apipi or _is_under(dest, apipi):
            raise SetupError("file path must be inside the workspace")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def prepare_workspace(
    workspace: Path, environment: dict[str, Any], *, max_bytes: int | None = None
) -> None:
    if environment.get("type") != "openai_hosted":
        return
    values = session_env_from(environment)
    files = inline_files_from(environment)
    packages = packages_from(environment)
    commands = setup_commands_from(environment)
    if packages.empty() and not commands and not values and not files:
        return
    workspace.mkdir(parents=True, exist_ok=True)
    apipi = workspace / ".apipi"
    apipi.mkdir(parents=True, exist_ok=True)
    write_inline_files(workspace, files, max_bytes=max_bytes)
    write_user_env(workspace, values)
    if packages.empty() and not commands:
        return
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


def run_host_setup(workspace: Path, *, extra_env: dict[str, str] | None = None) -> None:
    script = workspace / SETUP_SCRIPT
    if not script.is_file():
        return
    done = workspace / SETUP_DONE
    if done.is_file():
        return
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    try:
        result = subprocess.run(
            ["/bin/sh", str(script)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            env=env,
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


def provision_hosted(
    environment: dict[str, Any],
    *,
    run_mode: str,
    max_bytes: int | None = None,
) -> None:
    if environment.get("type") != "openai_hosted":
        return
    directory = environment.get("directory")
    if not isinstance(directory, str) or directory == "":
        return
    workspace = Path(directory)
    prepare_workspace(workspace, environment, max_bytes=max_bytes)
    if run_mode == "none":
        run_host_setup(workspace, extra_env=session_env_from(environment))
