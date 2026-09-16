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
    credential_id: str | None = None


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
        raw_cred = tool.get("credential_id")
        credential_id = raw_cred if isinstance(raw_cred, str) and raw_cred else None
        servers.append(
            McpHttpServer(
                server_label=label,
                server_url=url,
                headers={str(key): str(value) for key, value in headers.items()},
                credential_id=credential_id,
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
        credential_id=server.credential_id,
    )


def _norm_url(url: str) -> str:
    return url.rstrip("/")


def apply_vault_headers(
    servers: list[McpHttpServer],
    credentials: list[Any],
) -> list[McpHttpServer]:
    applied: list[McpHttpServer] = []
    for server in servers:
        chosen = None
        if server.credential_id:
            for cred in credentials:
                if str(cred.id) == server.credential_id:
                    chosen = cred
                    break
        else:
            matches = [
                cred
                for cred in credentials
                if _norm_url(cred.mcp_server_url) == _norm_url(server.server_url)
            ]
            if len(matches) > 1:
                raise McpConnectError(
                    f"mcp {server.server_label} matches several vault credentials"
                )
            if len(matches) == 1:
                chosen = matches[0]
        if chosen is None:
            applied.append(server)
            continue
        applied.append(
            McpHttpServer(
                server_label=server.server_label,
                server_url=server.server_url,
                headers={"Authorization": f"Bearer {chosen.token}"},
                credential_id=str(chosen.id),
            )
        )
    return applied


async def connect_mcp_http_tools(tools: list[Any] | None) -> list[McpHttpServer]:
    connected: list[McpHttpServer] = []
    for server in mcp_http_tools(tools):
        connected.append(await connect_mcp_http(server))
    return connected
