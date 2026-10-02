from typing import Any

from apipi.gateway.errors import ApiError
from apipi.worker.pi.settings_json import (
    builtin_tools_from_metadata,
    resolve_codemode,
)

ENV_NONE_TOOL_HELP = "type=none sessions allow function tools and HTTP MCP only"
ENV_NONE_BUILTIN_HELP = "built-in tools are not available for environment.type=none"
ENV_NONE_CODEMODE_HELP = "codemode requires built-in tools"


def reject_tools_for_env_none(tools: list[Any] | None) -> None:
    for tool in tools or []:
        if not isinstance(tool, dict):
            raise ApiError(
                "invalid_request",
                ENV_NONE_TOOL_HELP,
                code="tool_not_allowed",
            )
        kind = tool.get("type")
        if kind == "function":
            continue
        if kind == "mcp" and tool.get("server_url"):
            continue
        raise ApiError(
            "invalid_request",
            ENV_NONE_TOOL_HELP,
            code="tool_not_allowed",
        )


def reject_builtin_tools_for_env_none(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None = None,
) -> None:
    session_value = builtin_tools_from_metadata(session_metadata)
    agent_value = builtin_tools_from_metadata(agent_metadata)
    if session_value is None:
        effective = agent_value if agent_value is not None else "off"
    else:
        effective = session_value
    if effective == "on":
        raise ApiError(
            "invalid_request",
            ENV_NONE_BUILTIN_HELP,
            code="builtin_tools",
        )
    if resolve_codemode(session_metadata, agent_metadata) != "off":
        raise ApiError(
            "invalid_request",
            ENV_NONE_CODEMODE_HELP,
            code="builtin_tools",
        )


def validate_env_none(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    tools: list[Any] | None,
) -> None:
    reject_builtin_tools_for_env_none(session_metadata, agent_metadata)
    reject_tools_for_env_none(tools)


def env_type_of(environment: dict[str, Any] | None) -> str | None:
    if not isinstance(environment, dict):
        return None
    raw = environment.get("type")
    return raw if isinstance(raw, str) else None


def is_env_none(environment: dict[str, Any] | None) -> bool:
    return env_type_of(environment) == "none"
