import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from apipi.config import ConfigError, Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.version import PINNED_PI


class PiProc:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        stdin: asyncio.StreamWriter | None = None,
        stdout: asyncio.StreamReader | None = None,
        on_stop: Callable[[], None] | None = None,
        pull_artifacts: Callable[[], Awaitable[bytes]] | None = None,
        pull_workspace: Callable[[], Awaitable[bytes]] | None = None,
    ) -> None:
        self.process = process
        self._stdin = process.stdin if stdin is None else stdin
        self._stdout = process.stdout if stdout is None else stdout
        self._on_stop = on_stop
        self.pull_artifacts = pull_artifacts
        self.pull_workspace = pull_workspace
        self._buf = ""

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def send(self, payload: dict[str, Any]) -> None:
        if self._stdin is None:
            raise RuntimeError("pi stdin closed")
        self._stdin.write((json.dumps(payload) + "\n").encode())
        await self._stdin.drain()

    async def prompt(self, message: str) -> AsyncIterator[dict[str, Any]]:
        await self.send({"type": "prompt", "message": message})
        async for event in self._events():
            yield event
            if event.get("type") == "agent_settled":
                return

    async def abort(self) -> None:
        if not self.alive:
            return
        await self.send({"type": "abort"})

    async def _events(self) -> AsyncIterator[dict[str, Any]]:
        if self._stdout is None:
            return
        while self.alive:
            line = await self._stdout.readline()
            if not line:
                return
            raw = line.decode().rstrip("\r\n")
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
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
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        finally:
            if self._on_stop is not None:
                self._on_stop()
                self._on_stop = None


def pi_env(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    if settings.model_api_key:
        env["OPENAI_API_KEY"] = settings.model_api_key
    if settings.model_base_url:
        env["OPENAI_BASE_URL"] = settings.model_base_url
    env["APIPI_PINNED_PI"] = PINNED_PI
    if mcp_http:
        env["APIPI_MCP_SERVERS"] = ",".join(server.server_label for server in mcp_http)
        for index, server in enumerate(mcp_http):
            prefix = f"APIPI_MCP_{index}"
            env[f"{prefix}_LABEL"] = server.server_label
            env[f"{prefix}_URL"] = server.server_url
            for key, value in server.headers.items():
                safe = key.upper().replace("-", "_")
                env[f"{prefix}_{safe}"] = value
    if mcp_stdio:
        env["APIPI_MCP_STDIO"] = ",".join(server.server_label for server in mcp_stdio)
        for index, server in enumerate(mcp_stdio):
            prefix = f"APIPI_MCP_STDIO_{index}"
            env[f"{prefix}_LABEL"] = server.server_label
            env[f"{prefix}_COMMAND"] = server.command
            env[f"{prefix}_ARGS"] = "\x1f".join(server.args)
    return env


def pi_command_args(
    settings: Settings,
    *,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
) -> list[str]:
    command = settings.pi_command.split()
    args = [*command, "--mode", "rpc", "--no-session"]
    if not tools:
        args.append("--no-builtin-tools" if mcp_http or mcp_stdio else "--no-tools")
    if skill_dirs is not None:
        args.append("--no-skills")
        for path in skill_dirs:
            args.extend(["--skill", path])
    return args


async def spawn_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
) -> PiProc:
    if settings.run_mode == "jail":
        from apipi.pi.jail import spawn_jailed_pi

        return await spawn_jailed_pi(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        )
    if settings.run_mode == "microvm":
        from apipi.pi.microvm import spawn_microvm_pi

        return await spawn_microvm_pi(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        )
    if settings.run_mode != "host":
        raise ConfigError(f"APIPI_RUN_MODE={settings.run_mode} is not available")
    args = pi_command_args(
        settings,
        tools=tools,
        mcp_http=mcp_http,
        mcp_stdio=mcp_stdio,
        skill_dirs=skill_dirs,
    )
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=pi_env(settings, mcp_http, mcp_stdio),
    )
    return PiProc(process)
