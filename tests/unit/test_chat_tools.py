import pytest

from apipi.gateway.errors import ApiError
from apipi.services.chat_tools import (
    CHAT_TOOL_HELP,
    is_chat_profile,
    reject_disallowed_chat_tools,
)


def test_chat_profile_metadata() -> None:
    assert is_chat_profile({"apipi.session_kind": "chat"})
    assert not is_chat_profile({})
    assert not is_chat_profile(None)


def test_function_and_http_mcp_allowed() -> None:
    reject_disallowed_chat_tools(
        [
            {"type": "function", "name": "echo"},
            {
                "type": "mcp",
                "server_label": "search",
                "transport": {"type": "http", "server_url": "https://mcp.example/mcp"},
            },
        ]
    )


def test_stdio_mcp_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        reject_disallowed_chat_tools(
            [
                {
                    "type": "mcp",
                    "server_label": "playwright",
                    "transport": {"type": "stdio", "command": "npx"},
                }
            ]
        )
    assert exc.value.code == "chat_tool"
    assert exc.value.message == CHAT_TOOL_HELP


def test_unknown_tool_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        reject_disallowed_chat_tools([{"type": "bash"}])
    assert exc.value.code == "chat_tool"
