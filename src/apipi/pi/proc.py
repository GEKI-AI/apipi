import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.pi.version import PINNED_PI


class PiProc:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._buf = ""

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("pi stdin closed")
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def prompt(self, message: str) -> AsyncIterator[dict[str, Any]]:
        await self.send({"type": "prompt", "message": message})
        async for event in self._events():
            yield event
            if event.get("type") == "agent_settled":
                return

    async def _events(self) -> AsyncIterator[dict[str, Any]]:
        if self.process.stdout is None:
            return
        while self.alive:
            line = await self.process.stdout.readline()
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
        if not self.alive:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


def _pi_env(
    settings: Settings, mcp_http: list[McpHttpServer] | None = None
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
    return env


async def spawn_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
) -> PiProc:
    command = settings.pi_command.split()
    args = [*command, "--mode", "rpc", "--no-session"]
    if not tools:
        args.append("--no-tools")
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_pi_env(settings, mcp_http),
    )
    return PiProc(process)
