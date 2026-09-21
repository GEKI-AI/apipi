from typing import Any

from apipi.gateway.errors import ApiError
from apipi.worker.placement import CHAT, SESSION_KIND_KEY

CHAT_TOOL_HELP = "Chat sessions allow function tools and HTTP MCP only"


def is_chat_profile(metadata: dict[str, Any] | None) -> bool:
    return isinstance(metadata, dict) and metadata.get(SESSION_KIND_KEY) == CHAT


def reject_disallowed_chat_tools(tools: list[Any] | None) -> None:
    for tool in tools or []:
        if not isinstance(tool, dict):
            raise ApiError(
                "invalid_request",
                CHAT_TOOL_HELP,
                code="chat_tool",
            )
        kind = tool.get("type")
        if kind == "function":
            continue
        if kind == "mcp":
            transport = tool.get("transport")
            if isinstance(transport, dict) and transport.get("type") == "http":
                continue
        raise ApiError(
            "invalid_request",
            CHAT_TOOL_HELP,
            code="chat_tool",
        )
