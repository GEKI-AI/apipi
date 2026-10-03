from typing import Any

from apipi.common.errors import ApiError
from apipi.config import Settings

THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
THINKING_KEY = "apipi.thinking"
SYSTEM_PROMPT_KEY = "apipi.system_prompt"
CODEMODE_KEY = "apipi.codemode"
CODEMODE_MODES = frozenset({"off", "on", "only"})
BUILTIN_TOOLS_KEY = "apipi.builtin_tools"
BUILTIN_TOOLS_MODES = frozenset({"on", "off"})
THINKING_HELP = "apipi.thinking must be off, minimal, low, medium, high, xhigh, or max"
SYSTEM_PROMPT_HELP = "apipi.system_prompt must be a string"
CODEMODE_HELP = "codemode must be off, on, or only"
BUILTIN_TOOLS_HELP = "builtin_tools must be on or off"


def parse_thinking(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in THINKING_LEVELS:
        raise ApiError("invalid_request", THINKING_HELP, code="invalid_request")
    return value


def thinking_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or THINKING_KEY not in metadata:
        return None
    return parse_thinking(metadata.get(THINKING_KEY))


def parse_system_prompt(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("invalid_request", SYSTEM_PROMPT_HELP, code="invalid_request")
    text = value.strip()
    return text or None


def system_prompt_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or SYSTEM_PROMPT_KEY not in metadata:
        return None
    return parse_system_prompt(metadata.get(SYSTEM_PROMPT_KEY))


def parse_codemode(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in CODEMODE_MODES:
        raise ApiError("invalid_request", CODEMODE_HELP, code="invalid_request")
    return value


def codemode_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or CODEMODE_KEY not in metadata:
        return None
    return parse_codemode(metadata.get(CODEMODE_KEY))


def parse_builtin_tools(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in BUILTIN_TOOLS_MODES:
        raise ApiError("invalid_request", BUILTIN_TOOLS_HELP, code="invalid_request")
    return value


def builtin_tools_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or BUILTIN_TOOLS_KEY not in metadata:
        return None
    return parse_builtin_tools(metadata.get(BUILTIN_TOOLS_KEY))


def validate_pi_metadata(metadata: dict[str, Any] | None) -> None:
    thinking_from_metadata(metadata)
    system_prompt_from_metadata(metadata)
    codemode_from_metadata(metadata)
    builtin_tools_from_metadata(metadata)


def reject_client_thinking_key(metadata: dict[str, Any] | None) -> None:
    if isinstance(metadata, dict) and THINKING_KEY in metadata:
        raise ApiError(
            "invalid_request",
            "apipi.thinking was removed; set reasoning.effort "
            "(none, minimal, low, medium, high, xhigh, or max)",
            code="invalid_request",
        )


def public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict) or THINKING_KEY not in metadata:
        return metadata
    cleaned = dict(metadata)
    cleaned.pop(THINKING_KEY, None)
    return cleaned


def copy_inline_pi_metadata(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(session_metadata or {})
    agent = agent_metadata or {}
    for key in (THINKING_KEY, SYSTEM_PROMPT_KEY, CODEMODE_KEY, BUILTIN_TOOLS_KEY):
        if key not in out and key in agent:
            out[key] = agent[key]
    return out


_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


def effort_to_thinking(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _EFFORTS:
        raise ApiError(
            "invalid_request",
            "reasoning.effort must be none, minimal, low, medium, high, xhigh, or max",
            code="invalid_request",
        )
    return "off" if value == "none" else value


def thinking_to_effort(level: str | None) -> str | None:
    if level is None:
        return None
    return "none" if level == "off" else level


def reasoning_body(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {"effort": thinking_to_effort(thinking_from_metadata(metadata))}


def reject_reasoning_conflict(metadata: dict[str, Any] | None, effort: object) -> None:
    level = effort_to_thinking(effort)
    if level is None:
        return
    current = thinking_from_metadata(metadata)
    if current is not None and current != level:
        raise ApiError(
            "invalid_request",
            "reasoning.effort and apipi.thinking disagree",
            code="invalid_request",
        )


def apply_reasoning_effort(
    metadata: dict[str, Any] | None,
    effort: object,
    *,
    reset: bool = False,
) -> dict[str, Any]:
    out = dict(metadata or {})
    if effort is None and reset:
        out.pop(THINKING_KEY, None)
        return out
    level = effort_to_thinking(effort)
    if level is None:
        return out
    out[THINKING_KEY] = level
    return out


_STANDARD_THINKING = frozenset({"off", "minimal", "low", "medium", "high"})


def thinking_level_supported(levels: dict[str, str | None] | None, level: str) -> bool:
    if levels is None:
        return True
    if level in levels:
        return isinstance(levels[level], str)
    return level in _STANDARD_THINKING


def require_thinking_supported(
    settings: Settings, model: str | None, level: str | None
) -> None:
    if level is None or not isinstance(model, str) or not model:
        return
    from apipi.common.model_caps import registry_of

    caps = registry_of(settings.model_registry).get(model)
    if caps is None:
        return
    if level != "off" and caps.reasoning is False:
        raise ApiError(
            "invalid_request",
            f"model {model} does not support reasoning",
            code="invalid_request",
        )
    if not thinking_level_supported(caps.thinking_levels, level):
        raise ApiError(
            "invalid_request",
            "model "
            f"{model} does not support reasoning effort {thinking_to_effort(level)}",
            code="invalid_request",
        )


def resolve_thinking(
    settings: Settings,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    session = thinking_from_metadata(session_metadata)
    if session is not None:
        return session
    agent = thinking_from_metadata(agent_metadata)
    if agent is not None:
        return agent
    return settings.pi_thinking


def resolve_codemode(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    session = codemode_from_metadata(session_metadata)
    if session is not None:
        return session
    agent = codemode_from_metadata(agent_metadata)
    if agent is not None:
        return agent
    return "off"


def resolve_builtin_tools(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    session = builtin_tools_from_metadata(session_metadata)
    if session is not None:
        return session
    agent = builtin_tools_from_metadata(agent_metadata)
    if agent is not None:
        return agent
    return "on"


def reject_codemode_without_builtin_tools(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> None:
    if resolve_codemode(session_metadata, agent_metadata) == "off":
        return
    if resolve_builtin_tools(session_metadata, agent_metadata) == "off":
        raise ApiError(
            "invalid_request",
            "codemode requires built-in tools",
            code="builtin_tools",
        )


def function_tools(tools: list[Any] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    return [
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("type") == "function"
    ]


def effective_builtin_tools(
    environment: dict[str, Any] | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    if isinstance(environment, dict) and environment.get("type") == "none":
        return "off"
    return resolve_builtin_tools(session_metadata, agent_metadata)


def effective_codemode(
    builtin_tools: str,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    if builtin_tools == "off":
        return "off"
    return resolve_codemode(session_metadata, agent_metadata)
