import pytest

from apipi.gateway.errors import ApiError
from apipi.services.env_none import (
    ENV_NONE_BUILTIN_HELP,
    ENV_NONE_CODEMODE_HELP,
    ENV_NONE_TOOL_HELP,
    is_env_none,
    reject_builtin_tools_for_env_none,
    reject_tools_for_env_none,
    validate_env_none,
)


def test_env_none_detection() -> None:
    assert is_env_none({"type": "none"})
    assert not is_env_none({"type": "openai_hosted"})
    assert not is_env_none({})
    assert not is_env_none(None)


def test_function_and_http_mcp_allowed() -> None:
    reject_tools_for_env_none(
        [
            {"type": "function", "name": "echo"},
            {
                "type": "mcp",
                "server_label": "search",
                "server_url": "https://mcp.example/mcp",
            },
        ]
    )


def test_stdio_mcp_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        reject_tools_for_env_none(
            [
                {
                    "type": "mcp",
                    "server_label": "playwright",
                    "transport": {"type": "stdio", "command": "npx"},
                }
            ]
        )
    assert exc.value.code == "tool_not_allowed"
    assert exc.value.message == ENV_NONE_TOOL_HELP


def test_unknown_tool_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        reject_tools_for_env_none([{"type": "bash"}])
    assert exc.value.code == "tool_not_allowed"


def test_builtin_tools_on_rejected_for_env_none() -> None:
    with pytest.raises(ApiError) as exc:
        reject_builtin_tools_for_env_none({"apipi.builtin_tools": "on"}, None)
    assert exc.value.code == "builtin_tools"
    assert exc.value.message == ENV_NONE_BUILTIN_HELP


def test_builtin_tools_off_and_absent_allowed_for_env_none() -> None:
    reject_builtin_tools_for_env_none({"apipi.builtin_tools": "off"}, None)
    reject_builtin_tools_for_env_none({}, None)
    reject_builtin_tools_for_env_none(None, None)


def test_session_over_agent_wins_for_env_none() -> None:
    with pytest.raises(ApiError) as exc:
        reject_builtin_tools_for_env_none(
            {"apipi.builtin_tools": "on"}, {"apipi.builtin_tools": "off"}
        )
    assert exc.value.code == "builtin_tools"
    reject_builtin_tools_for_env_none(
        {"apipi.builtin_tools": "off"}, {"apipi.builtin_tools": "on"}
    )


def test_codemode_rejected_for_env_none() -> None:
    with pytest.raises(ApiError) as exc:
        reject_builtin_tools_for_env_none({"apipi.codemode": "on"}, None)
    assert exc.value.code == "builtin_tools"
    assert exc.value.message == ENV_NONE_CODEMODE_HELP
    with pytest.raises(ApiError):
        reject_builtin_tools_for_env_none({"apipi.codemode": "only"}, None)


def test_validate_env_none_combines_checks() -> None:
    validate_env_none(
        {"apipi.builtin_tools": "off"},
        None,
        [{"type": "function", "name": "echo"}],
    )
    with pytest.raises(ApiError) as exc:
        validate_env_none({"apipi.builtin_tools": "off"}, None, [{"type": "bash"}])
    assert exc.value.code == "tool_not_allowed"
    with pytest.raises(ApiError) as exc:
        validate_env_none({"apipi.builtin_tools": "on"}, None, [])
    assert exc.value.code == "builtin_tools"
