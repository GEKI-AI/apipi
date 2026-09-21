import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.worker.pi.orphan import host_pi_stamp
from apipi.worker.pi.version import PINNED_PI

log = logging.getLogger("apipi.worker.pi")


def _is_session_leader(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def _killpg(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, sig)


def _kill_pgrp_members(pgid: int, sig: int) -> None:
    mypid = os.getpid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == mypid:
            continue
        try:
            with open(f"/proc/{name}/stat") as fh:
                rest = fh.read().split(")")[-1].split()
            if int(rest[2]) != pgid:
                continue
            os.kill(pid, sig)
        except (
            FileNotFoundError,
            ProcessLookupError,
            PermissionError,
            IndexError,
            ValueError,
        ):
            continue


class PiProc:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        stdin: asyncio.StreamWriter | None = None,
        stdout: asyncio.StreamReader | None = None,
        on_stop: Callable[[], None] | None = None,
        broker: Any | None = None,
        pull_artifacts: Callable[[], Awaitable[bytes]] | None = None,
        pull_workspace: Callable[[], Awaitable[bytes]] | None = None,
        pull_session: Callable[[], Awaitable[bytes]] | None = None,
        pull_metrics: Callable[[], Awaitable[bytes]] | None = None,
        vm_id: str | None = None,
        process_group: bool = False,
        scratch_dir: str | None = None,
        stderr_task: asyncio.Task[None] | None = None,
    ) -> None:
        self.process = process
        self._stdin = process.stdin if stdin is None else stdin
        self._stdout = process.stdout if stdout is None else stdout
        self._on_stop = on_stop
        self.broker = broker
        self.pull_artifacts = pull_artifacts
        self.pull_workspace = pull_workspace
        self.pull_session = pull_session
        self.pull_metrics = pull_metrics
        self.vm_id = vm_id
        self.process_group = process_group
        self.scratch_dir = scratch_dir
        self._stderr_task = stderr_task
        self._buf = b""

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def send(self, payload: dict[str, Any]) -> None:
        if self._stdin is None:
            raise RuntimeError("pi stdin closed")
        self._stdin.write((json.dumps(payload) + "\n").encode())
        await self._stdin.drain()

    async def prompt(self, message: str) -> AsyncIterator[dict[str, Any]]:
        log.info("pi prompt")
        await self.send({"type": "prompt", "message": message})
        first = True
        async for event in self._events():
            kind = event.get("type")
            if first:
                log.info("pi event", extra={"type": kind})
                first = False
            yield event
            if kind == "agent_settled":
                log.info("pi settled")
                return

    async def abort(self) -> None:
        if not self.alive:
            return
        await self.send({"type": "abort"})

    async def _events(self) -> AsyncIterator[dict[str, Any]]:
        if self._stdout is None:
            return
        while True:
            while b"\n" not in self._buf:
                chunk = await self._stdout.read(65536)
                if not chunk:
                    return
                self._buf += chunk
            raw, self._buf = self._buf.split(b"\n", 1)
            try:
                text = raw.rstrip(b"\r").decode()
            except UnicodeDecodeError:
                continue
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                log.warning("pi stdout not json", extra={"line": text[:200]})
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "response":
                continue
            yield event

    async def terminate(self) -> None:
        try:
            if not self.alive:
                return
            pid = self.process.pid
            group = self.process_group and pid is not None and _is_session_leader(pid)
            if group and pid is not None:
                _killpg(pid, signal.SIGTERM)
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=2)
            if group and pid is not None:
                _killpg(pid, signal.SIGKILL)
                _kill_pgrp_members(pid, signal.SIGKILL)
            elif self.process.returncode is None:
                self.process.kill()
            if self.process.returncode is None:
                await self.process.wait()
        finally:
            if self._on_stop is not None:
                self._on_stop()
                self._on_stop = None
            if self.broker is not None:
                await self.broker.stop()
                self.broker = None
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                self._stderr_task = None
            if self.scratch_dir is not None:
                shutil.rmtree(self.scratch_dir, ignore_errors=True)
                self.scratch_dir = None


def pi_env(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    *,
    api_key: str | None = None,
    broker: Any | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    from apipi.worker.pi.broker import DUMMY_KEY
    from apipi.worker.pi.model_host import pi_agent_dir

    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    key = api_key if api_key else settings.model_api_key_overwrite
    if broker is not None:
        env["OPENAI_API_KEY"] = DUMMY_KEY
        env["OPENAI_BASE_URL"] = broker.openai_base_url
    else:
        if key:
            env["OPENAI_API_KEY"] = key
        else:
            env.pop("OPENAI_API_KEY", None)
        if settings.model_base_url:
            env["OPENAI_BASE_URL"] = settings.model_base_url
    env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir(settings))
    env["APIPI_PINNED_PI"] = PINNED_PI
    env.update(host_pi_stamp())
    if mcp_http:
        env["APIPI_MCP_SERVERS"] = ",".join(server.server_label for server in mcp_http)
        for index, server in enumerate(mcp_http):
            prefix = f"APIPI_MCP_{index}"
            env[f"{prefix}_LABEL"] = server.server_label
            if broker is not None:
                env[f"{prefix}_URL"] = broker.mcp_url(str(index))
            else:
                env[f"{prefix}_URL"] = server.server_url
                for header, value in server.headers.items():
                    safe = header.upper().replace("-", "_")
                    env[f"{prefix}_{safe}"] = value
    if mcp_stdio:
        env["APIPI_MCP_STDIO"] = ",".join(server.server_label for server in mcp_stdio)
        for index, server in enumerate(mcp_stdio):
            prefix = f"APIPI_MCP_STDIO_{index}"
            env[f"{prefix}_LABEL"] = server.server_label
            env[f"{prefix}_COMMAND"] = server.command
            env[f"{prefix}_ARGS"] = "\x1f".join(server.args)
            if server.cwd:
                env[f"{prefix}_CWD"] = server.cwd
    if extra_env:
        env.update(extra_env)
    if settings.pi_mem_mib is not None:
        env["NODE_OPTIONS"] = f"--max-old-space-size={settings.pi_mem_mib}"
    return env


def pi_command_args(
    settings: Settings,
    *,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    session_file: str | None = None,
    extension: str | None = None,
) -> list[str]:
    from apipi.worker.pi.model_host import PI_PROVIDER

    command = settings.pi_command.split()
    args = [*command, "--mode", "rpc"]
    if session_file:
        args.extend(["--session", session_file])
    else:
        args.append("--no-session")
    if model:
        args.extend(["--provider", PI_PROVIDER, "--model", model])
    if instructions:
        args.extend(["--append-system-prompt", instructions])
    if not settings.pi_auto_compact:
        args.append("--no-auto-compact")
    if not tools:
        args.append("--no-builtin-tools" if mcp_http or mcp_stdio else "--no-tools")
    if skill_dirs is not None:
        args.append("--no-skills")
        for path in skill_dirs:
            args.extend(["--skill", path])
    if extension:
        args.extend(["--extension", extension])
    return args


async def spawn_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    api_key: str | None = None,
    mem_mib: int | None = None,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> PiProc:
    from apipi.worker.pi.isolation import load_isolation

    return await load_isolation(settings.run_mode).spawn(
        settings,
        cwd=cwd,
        tools=tools,
        mcp_http=mcp_http,
        mcp_stdio=mcp_stdio,
        skill_dirs=skill_dirs,
        model=model,
        instructions=instructions,
        api_key=api_key,
        mem_mib=mem_mib,
        image=image,
        extra_env=extra_env,
    )
