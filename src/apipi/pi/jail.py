import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from apipi.config import ConfigError, Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.proc import PiProc, pi_command_args, pi_env

PASTA_DNS = "169.254.254.254"

_resolv: Path | None = None


def jail_binaries() -> tuple[str, str]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ConfigError("APIPI_RUN_MODE=jail requires bwrap")
    pasta = shutil.which("pasta")
    if pasta is None:
        raise ConfigError("APIPI_RUN_MODE=jail requires pasta")
    return bwrap, pasta


def _self_cgroup_path() -> Path | None:
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None
    rel: str | None = None
    for line in lines:
        if line.startswith("0::"):
            rel = line[3:].lstrip("/")
            break
    if rel is None:
        return None
    path = Path("/sys/fs/cgroup") / rel
    if path.is_dir():
        return path
    return None


def cgroup_v2_available() -> bool:
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        return False
    parent = _self_cgroup_path()
    if parent is None or not os.access(parent, os.W_OK):
        return False
    probe = parent / f".apipi-probe-{os.getpid()}"
    try:
        probe.mkdir()
        probe.rmdir()
    except OSError:
        return False
    return True


def require_jail() -> None:
    jail_binaries()
    if not cgroup_v2_available():
        raise ConfigError("APIPI_RUN_MODE=jail requires cgroup v2")


def attach_cgroup(pid: int) -> None:
    parent = _self_cgroup_path()
    if parent is None:
        raise OSError("cgroup v2 parent missing")
    path = parent / f"apipi-{pid}"
    path.mkdir()
    (path / "cgroup.procs").write_text(str(pid))


def resolv_conf() -> Path:
    global _resolv
    if _resolv is not None and _resolv.is_file():
        return _resolv
    fd, name = tempfile.mkstemp(prefix="apipi-jail-resolv-")
    with os.fdopen(fd, "w") as handle:
        handle.write(f"nameserver {PASTA_DNS}\n")
    _resolv = Path(name)
    return _resolv


def jail_argv(
    pi_args: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    bwrap: str,
    pasta: str,
    resolv: str,
) -> list[str]:
    argv = [
        pasta,
        "--foreground",
        "--quiet",
        "--config-net",
        "--map-host-loopback",
        "none",
        "--tcp-ports",
        "none",
        "--udp-ports",
        "none",
        "--tcp-ns",
        "none",
        "--udp-ns",
        "none",
        "--dns-forward",
        PASTA_DNS,
        "--",
        bwrap,
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        resolv,
        "/etc/resolv.conf",
    ]
    if cwd:
        argv.extend(["--bind", cwd, cwd, "--chdir", cwd])
    else:
        argv.extend(["--chdir", "/tmp"])
    argv.append("--clearenv")
    for key, value in env.items():
        if key == "DATABASE_URL":
            continue
        argv.extend(["--setenv", key, value])
    argv.extend(pi_args)
    return argv


def _start_mcp_from_env() -> None:
    labels = os.environ.get("APIPI_MCP_STDIO")
    if not labels:
        return
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    for index, _label in enumerate(labels.split(",")):
        prefix = f"APIPI_MCP_STDIO_{index}"
        command = env.get(f"{prefix}_COMMAND")
        if not command:
            continue
        raw_args = env.get(f"{prefix}_ARGS", "")
        args = raw_args.split("\x1f") if raw_args else []
        subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )


def inner_main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    _start_mcp_from_env()
    if not args:
        raise SystemExit("missing pi command")
    os.execvpe(args[0], args, os.environ)


async def spawn_jailed_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
) -> PiProc:
    require_jail()
    bwrap, pasta = jail_binaries()
    cwd_abs = str(Path(cwd).resolve()) if cwd else None
    env = pi_env(settings, mcp_http, mcp_stdio)
    env["HOME"] = cwd_abs or "/tmp"
    pi_args = pi_command_args(
        settings,
        tools=tools,
        mcp_http=mcp_http,
        mcp_stdio=mcp_stdio,
        skill_dirs=skill_dirs,
    )
    if mcp_stdio:
        pi_args = [sys.executable, "-m", "apipi.pi.jail", *pi_args]
    argv = jail_argv(
        pi_args,
        cwd=cwd_abs,
        env=env,
        bwrap=bwrap,
        pasta=pasta,
        resolv=str(resolv_conf()),
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise ConfigError("APIPI_RUN_MODE=jail cannot start") from exc
    pid = process.pid
    if pid is None:
        process.kill()
        await process.wait()
        raise ConfigError("APIPI_RUN_MODE=jail cannot start")
    try:
        attach_cgroup(pid)
    except OSError as exc:
        process.kill()
        await process.wait()
        raise ConfigError("APIPI_RUN_MODE=jail requires cgroup v2") from exc
    return PiProc(process)


if __name__ == "__main__":
    inner_main()
