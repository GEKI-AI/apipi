from typing import Any

from apipi.gateway.errors import ApiError

ENV_NONE_TOOL_HELP = "type=none sessions allow function tools and HTTP MCP only"


def reject_tools_for_env_none(tools: list[Any] | None) -> None:
    for tool in tools or []:
        if not isinstance(tool, dict):
            raise ApiError(
                "invalid_request",
                ENV_NONE_TOOL_HELP,
                code="chat_tool",
            )
        kind = tool.get("type")
        if kind == "function":
            continue
        if kind == "mcp" and tool.get("server_url"):
            continue
        raise ApiError(
            "invalid_request",
            ENV_NONE_TOOL_HELP,
            code="chat_tool",
        )


def env_type_of(environment: dict[str, Any] | None) -> str | None:
    if not isinstance(environment, dict):
        return None
    raw = environment.get("type")
    return raw if isinstance(raw, str) else None


def is_env_none(environment: dict[str, Any] | None) -> bool:
    return env_type_of(environment) == "none"
