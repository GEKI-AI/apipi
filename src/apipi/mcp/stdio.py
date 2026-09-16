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
    cwd: str | None = None


def mcp_stdio_tools(
    tools: list[Any] | None,
) -> list[tuple[str, str, list[str], str | None]]:
    servers: list[tuple[str, str, list[str], str | None]] = []
    if not tools:
        return servers
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        transport = tool.get("transport")
        if not isinstance(transport, dict) or transport.get("type") != "stdio":
            continue
        command = transport.get("command")
        label = tool.get("server_label")
        if not isinstance(command, str) or not command or not isinstance(label, str):
            continue
        raw_args = transport.get("args")
        args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
        raw_cwd = transport.get("cwd")
        cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
        servers.append((label, command, args, cwd))
    return servers


async def start_mcp_stdio(
    label: str, command: str, args: list[str], *, cwd: str | None = None
) -> McpStdioServer:
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
            cwd=cwd,
        )
    except OSError as exc:
        raise McpConnectError(f"mcp {label} failed") from exc
    await asyncio.sleep(0.05)
    if process.returncode is not None:
        raise McpConnectError(f"mcp {label} failed")
    return McpStdioServer(
        server_label=label, command=command, args=args, process=process, cwd=cwd
    )


async def start_mcp_stdio_tools(
    tools: list[Any] | None, *, on_host: bool = True
) -> list[McpStdioServer]:
    started: list[McpStdioServer] = []
    try:
        for label, command, args, cwd in mcp_stdio_tools(tools):
            if on_host:
                started.append(await start_mcp_stdio(label, command, args, cwd=cwd))
            else:
                started.append(
                    McpStdioServer(
                        server_label=label,
                        command=command,
                        args=args,
                        process=None,
                        cwd=cwd,
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
