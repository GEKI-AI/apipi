import sys
from pathlib import Path
from typing import Any

import pytest

from apipi.config import (
    IMPLEMENTED_RUN_MODES,
    ConfigError,
    RunMode,
    Settings,
    require_run_mode,
)
from apipi.mcp.stdio import McpStdioServer, start_mcp_stdio_tools
from apipi.pi.jail import (
    PASTA_DNS,
    inner_main,
    jail_argv,
    require_jail,
    spawn_jailed_pi,
)
from apipi.pi.proc import spawn_pi


def _settings(
    *, run_mode: RunMode = "jail", sessions_dir: str | None = None
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        pi_command="pi",
        sessions_dir=sessions_dir,
    )


def _which_ok(name: str) -> str:
    return f"/usr/bin/{name}"


def test_jail_is_implemented() -> None:
    assert "jail" in IMPLEMENTED_RUN_MODES


def test_require_jail_missing_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apipi.pi.jail.shutil.which",
        lambda name: None if name == "bwrap" else _which_ok(name),
    )
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: True)
    with pytest.raises(ConfigError, match="bwrap"):
        require_jail()
    with pytest.raises(ConfigError, match="bwrap"):
        require_run_mode("jail")


def test_require_jail_missing_pasta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apipi.pi.jail.shutil.which",
        lambda name: None if name == "pasta" else _which_ok(name),
    )
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: True)
    with pytest.raises(ConfigError, match="pasta"):
        require_jail()


def test_require_jail_missing_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.pi.jail.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: False)
    with pytest.raises(ConfigError, match="cgroup v2"):
        require_jail()


def test_jail_argv_uses_pasta_and_bwrap(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    argv = jail_argv(
        ["pi", "--mode", "rpc", "--no-session"],
        cwd=cwd,
        env={"OPENAI_API_KEY": "k", "DATABASE_URL": "postgresql://x"},
        bwrap="/usr/bin/bwrap",
        pasta="/usr/bin/pasta",
        resolv="/tmp/resolv.conf",
        sessions_dir=str(tmp_path.parent),
    )
    assert argv[0] == "/usr/bin/pasta"
    assert "--foreground" in argv
    assert "--config-net" in argv
    assert "--share-net" not in argv
    assert "--unshare-net" not in argv
    assert "--map-host-loopback" in argv
    assert argv[argv.index("--map-host-loopback") + 1] == "none"
    assert "--dns-forward" in argv
    assert argv[argv.index("--dns-forward") + 1] == PASTA_DNS
    assert "/usr/bin/bwrap" in argv
    bind_at = argv.index("--bind")
    assert argv[bind_at + 1] == cwd
    assert argv[bind_at + 2] == cwd
    assert "--chdir" in argv
    assert argv[argv.index("--chdir") + 1] == cwd
    assert "DATABASE_URL" not in argv
    assert "OPENAI_API_KEY" in argv
    assert argv[-4:] == ["pi", "--mode", "rpc", "--no-session"]
    tmpfs_dirs = [
        argv[index + 1] for index, item in enumerate(argv) if item == "--tmpfs"
    ]
    assert str(tmp_path.parent) in tmpfs_dirs


def test_jail_argv_hides_sessions_dir_before_bind(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    cwd = sessions / "tenant" / "session"
    cwd.mkdir(parents=True)
    argv = jail_argv(
        ["pi", "--mode", "rpc"],
        cwd=str(cwd),
        env={},
        bwrap="/usr/bin/bwrap",
        pasta="/usr/bin/pasta",
        resolv="/tmp/resolv.conf",
        sessions_dir=str(sessions),
    )
    tmpfs_at = [index for index, item in enumerate(argv) if item == "--tmpfs"]
    sessions_tmpfs = next(
        index for index in tmpfs_at if argv[index + 1] == str(sessions)
    )
    bind_at = argv.index("--bind")
    assert argv[bind_at + 1] == str(cwd)
    assert argv[bind_at + 2] == str(cwd)
    assert sessions_tmpfs < bind_at


class _Process:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def test_spawn_pi_jail_uses_pasta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "session"
    cwd.mkdir()
    monkeypatch.setattr("apipi.pi.jail.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: True)
    monkeypatch.setattr("apipi.pi.jail.attach_cgroup", lambda *_args, **_kwargs: None)
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **kwargs: Any) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr("apipi.pi.jail.asyncio.create_subprocess_exec", fake_exec)
    sessions = tmp_path / "sessions"
    proc = await spawn_pi(
        _settings(sessions_dir=str(sessions)), cwd=str(cwd), tools=True
    )
    assert proc.process.pid == 4242
    args = captured["args"]
    assert args[0] == "/usr/bin/pasta"
    assert "--share-net" not in args
    assert str(cwd.resolve()) in args
    tmpfs_dirs = [args[i + 1] for i, item in enumerate(args) if item == "--tmpfs"]
    assert str(sessions.resolve()) in tmpfs_dirs
    assert "DATABASE_URL" not in args
    env = captured["kwargs"]["env"]
    assert "DATABASE_URL" not in env


async def test_spawn_jail_does_not_fallback_to_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apipi.pi.jail.shutil.which", lambda _name: None)
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr("apipi.pi.jail.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="bwrap"):
        await spawn_pi(_settings(), cwd=None, tools=True)
    assert called is False


async def test_spawn_jail_stdio_runs_inside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "session"
    cwd.mkdir()
    monkeypatch.setattr("apipi.pi.jail.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.pi.jail.cgroup_v2_available", lambda: True)
    monkeypatch.setattr("apipi.pi.jail.attach_cgroup", lambda *_args, **_kwargs: None)
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **kwargs: Any) -> _Process:
        captured["args"] = args
        return _Process()

    monkeypatch.setattr("apipi.pi.jail.asyncio.create_subprocess_exec", fake_exec)
    stdio = [
        McpStdioServer(
            server_label="local", command="npx", args=["-y", "mcp"], process=None
        )
    ]
    await spawn_jailed_pi(_settings(), cwd=str(cwd), tools=True, mcp_stdio=stdio)
    args = list(captured["args"])
    assert args[0] == "/usr/bin/pasta"
    assert sys.executable in args
    assert args[args.index("-m") + 1] == "apipi.pi.jail"


async def test_stdio_not_started_on_host_in_jail() -> None:
    servers = await start_mcp_stdio_tools(
        [
            {
                "type": "mcp",
                "server_label": "local",
                "command": "mcp-stdio-does-not-exist",
                "args": [],
            }
        ],
        on_host=False,
    )
    assert len(servers) == 1
    assert servers[0].server_label == "local"
    assert servers[0].process is None


def test_inner_starts_mcp_then_execs_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> None:
        started.append(cmd)

    executed: dict[str, Any] = {}

    def fake_exec(file: str, args: list[str], env: dict[str, str]) -> None:
        executed["file"] = file
        executed["args"] = args
        executed["env"] = env
        raise OSError("exec")

    monkeypatch.setattr("apipi.pi.jail.subprocess.Popen", fake_popen)
    monkeypatch.setattr("apipi.pi.jail.os.execvpe", fake_exec)
    monkeypatch.setenv("APIPI_MCP_STDIO", "local")
    monkeypatch.setenv("APIPI_MCP_STDIO_0_COMMAND", "npx")
    monkeypatch.setenv("APIPI_MCP_STDIO_0_ARGS", "-y\x1fmcp")
    with pytest.raises(OSError, match="exec"):
        inner_main(["pi", "--mode", "rpc"])
    assert started == [["npx", "-y", "mcp"]]
    assert executed["file"] == "pi"
    assert executed["args"] == ["pi", "--mode", "rpc"]
