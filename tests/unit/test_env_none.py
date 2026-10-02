import pytest

from apipi.gateway.errors import ApiError
from apipi.services.env_none import (
    ENV_NONE_TOOL_HELP,
    is_env_none,
    reject_tools_for_env_none,
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
    assert exc.value.code == "chat_tool"
    assert exc.value.message == ENV_NONE_TOOL_HELP


def test_unknown_tool_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        reject_tools_for_env_none([{"type": "bash"}])
    assert exc.value.code == "chat_tool"
