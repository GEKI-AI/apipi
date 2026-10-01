from dataclasses import dataclass
from typing import Any

from apipi.mcp.guard import McpConnectError

__all__ = ["McpConnectError", "McpHttpServer"]

_ENV_PATTERN = "${"


@dataclass(frozen=True)
class McpHttpServer:
    server_label: str
    server_url: str
    headers: dict[str, str]
    credential_id: str | None = None
    allowed_tools: tuple[str, ...] = ()


def allowed_tool_names(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list):
        return tuple(item for item in raw if isinstance(item, str) and item)
    if isinstance(raw, dict):
        names = raw.get("tool_names")
        if isinstance(names, list):
            return tuple(item for item in names if isinstance(item, str) and item)
    return ()


def mcp_http_tools(tools: list[Any] | None) -> list[McpHttpServer]:
    servers: list[McpHttpServer] = []
    if not tools:
        return servers
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        if "transport" in tool:
            raise McpConnectError(
                "mcp tools use the flat OpenAI shape with top-level server_url; "
                "nested transport was removed"
            )
        url = tool.get("server_url")
        label = tool.get("server_label")
        if not isinstance(url, str) or not url or not isinstance(label, str):
            continue
        raw_headers = tool.get("headers")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        for value in headers.values():
            if _ENV_PATTERN in str(value):
                raise McpConnectError(
                    f"mcp {label} headers must not contain ${{...}}; "
                    "store the secret in a vault credential bound to "
                    "mcp_server_url and attach vault_ids on the session"
                )
        raw_cred = tool.get("credential_id")
        credential_id = raw_cred if isinstance(raw_cred, str) and raw_cred else None
        servers.append(
            McpHttpServer(
                server_label=label,
                server_url=url,
                headers={str(key): str(value) for key, value in headers.items()},
                credential_id=credential_id,
                allowed_tools=allowed_tool_names(tool.get("allowed_tools")),
            )
        )
    return servers


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
                allowed_tools=server.allowed_tools,
            )
        )
    return applied
