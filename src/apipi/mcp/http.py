import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from apipi import __version__

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpConnectError(Exception):
    pass


@dataclass(frozen=True)
class McpHttpServer:
    server_label: str
    server_url: str
    headers: dict[str, str]


def mcp_http_tools(tools: list[Any] | None) -> list[McpHttpServer]:
    servers: list[McpHttpServer] = []
    if not tools:
        return servers
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        url = tool.get("server_url")
        label = tool.get("server_label")
        if not isinstance(url, str) or not url or not isinstance(label, str):
            continue
        raw_headers = tool.get("headers")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        servers.append(
            McpHttpServer(
                server_label=label,
                server_url=url,
                headers={str(key): str(value) for key, value in headers.items()},
            )
        )
    return servers


def expand_headers(headers: dict[str, str]) -> dict[str, str]:
    expanded: dict[str, str] = {}
    for key, value in headers.items():

        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            found = os.environ.get(name)
            if found is None:
                raise McpConnectError(f"missing env {name}")
            return found

        expanded[key] = _ENV.sub(_sub, value)
    return expanded


async def connect_mcp_http(server: McpHttpServer) -> McpHttpServer:
    headers = expand_headers(server.headers)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "apipi", "version": __version__},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                server.server_url,
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    **headers,
                },
            )
            response.raise_for_status()
            body = response.json()
    except McpConnectError:
        raise
    except Exception as exc:
        raise McpConnectError(f"mcp {server.server_label} failed") from exc
    if not isinstance(body, dict) or body.get("error") is not None:
        raise McpConnectError(f"mcp {server.server_label} failed")
    return McpHttpServer(
        server_label=server.server_label,
        server_url=server.server_url,
        headers=headers,
    )


async def connect_mcp_http_tools(tools: list[Any] | None) -> list[McpHttpServer]:
    connected: list[McpHttpServer] = []
    for server in mcp_http_tools(tools):
        connected.append(await connect_mcp_http(server))
    return connected
