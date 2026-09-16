import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.version import PINNED_PI

log = logging.getLogger("apipi.pi")


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
        pull_session: Callable[[], Awaitable[bytes]] | None = None,
    ) -> None:
        self.process = process
        self._stdin = process.stdin if stdin is None else stdin
        self._stdout = process.stdout if stdout is None else stdout
        self._on_stop = on_stop
        self.pull_artifacts = pull_artifacts
        self.pull_workspace = pull_workspace
        self.pull_session = pull_session
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
    *,
    api_key: str | None = None,
) -> dict[str, str]:
    from apipi.pi.model_host import pi_agent_dir

    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    key = api_key if api_key else settings.model_api_key_overwrite
    if key:
        env["OPENAI_API_KEY"] = key
    else:
        env.pop("OPENAI_API_KEY", None)
    if settings.model_base_url:
        env["OPENAI_BASE_URL"] = settings.model_base_url
    env["PI_CODING_AGENT_DIR"] = str(pi_agent_dir(settings))
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
    model: str | None = None,
    instructions: str | None = None,
    session_file: str | None = None,
) -> list[str]:
    from apipi.pi.model_host import PI_PROVIDER

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
) -> PiProc:
    from apipi.pi.isolation import load_isolation

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
    )
