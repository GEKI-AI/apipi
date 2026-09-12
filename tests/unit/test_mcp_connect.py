import pytest

from apipi.mcp.http import (
    McpConnectError,
    McpHttpServer,
    expand_headers,
    mcp_http_tools,
)


def test_mcp_http_tools_skips_stdio_and_functions() -> None:
    servers = mcp_http_tools(
        [
            {"type": "function", "name": "echo"},
            {"type": "mcp", "server_label": "local", "command": "npx"},
            {
                "type": "mcp",
                "server_label": "tavily",
                "server_url": "https://mcp.tavily.com/mcp",
                "headers": {"Authorization": "Bearer ${TAVILY_API_KEY}"},
            },
        ]
    )
    assert len(servers) == 1
    assert servers[0].server_label == "tavily"


def test_expand_headers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    assert expand_headers({"Authorization": "Bearer ${TAVILY_API_KEY}"}) == {
        "Authorization": "Bearer secret"
    }


def test_expand_headers_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    with pytest.raises(McpConnectError, match="missing env"):
        expand_headers({"Authorization": "Bearer ${MISSING_KEY}"})


def test_mcp_http_server_is_frozen() -> None:
    server = McpHttpServer(server_label="x", server_url="http://x", headers={})
    assert server.server_label == "x"
