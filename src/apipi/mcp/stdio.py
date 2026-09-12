import asyncio
import os
from dataclasses import dataclass
from typing import Any

from apipi.mcp.http import McpConnectError


@dataclass
class McpStdioServer:
    server_label: str
    command: str
    args: list[str]
    process: asyncio.subprocess.Process | None = None


def mcp_stdio_tools(tools: list[Any] | None) -> list[tuple[str, str, list[str]]]:
    servers: list[tuple[str, str, list[str]]] = []
    if not tools:
        return servers
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        command = tool.get("command")
        label = tool.get("server_label")
        if not isinstance(command, str) or not command or not isinstance(label, str):
            continue
        raw_args = tool.get("args")
        args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
        servers.append((label, command, args))
    return servers


async def start_mcp_stdio(label: str, command: str, args: list[str]) -> McpStdioServer:
    try:
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError as exc:
        raise McpConnectError(f"mcp {label} failed") from exc
    await asyncio.sleep(0.05)
    if process.returncode is not None:
        raise McpConnectError(f"mcp {label} failed")
    return McpStdioServer(
        server_label=label, command=command, args=args, process=process
    )


async def start_mcp_stdio_tools(
    tools: list[Any] | None, *, on_host: bool = True
) -> list[McpStdioServer]:
    started: list[McpStdioServer] = []
    try:
        for label, command, args in mcp_stdio_tools(tools):
            if on_host:
                started.append(await start_mcp_stdio(label, command, args))
            else:
                started.append(
                    McpStdioServer(
                        server_label=label, command=command, args=args, process=None
                    )
                )
    except McpConnectError:
        await stop_mcp_stdio(started)
        raise
    return started


async def stop_mcp_stdio(servers: list[McpStdioServer]) -> None:
    for server in servers:
        process = server.process
        if process is None or process.returncode is not None:
            continue
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
