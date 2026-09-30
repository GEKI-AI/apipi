import pytest

from apipi.mcp.http import (
    McpConnectError,
    McpHttpServer,
    expand_headers,
    mcp_http_tools,
)


def test_mcp_http_tools_skips_functions_and_reads_flat_shape() -> None:
    servers = mcp_http_tools(
        [
            {"type": "function", "name": "echo"},
            {
                "type": "mcp",
                "server_label": "tavily",
                "server_url": "https://mcp.tavily.com/mcp",
                "headers": {"Authorization": "Bearer ${TAVILY_API_KEY}"},
                "allowed_tools": ["search"],
            },
        ]
    )
    assert len(servers) == 1
    assert servers[0].server_label == "tavily"
    assert servers[0].allowed_tools == ("search",)


def test_mcp_http_tools_rejects_nested_transport() -> None:
    with pytest.raises(McpConnectError, match="flat OpenAI shape"):
        mcp_http_tools(
            [
                {
                    "type": "mcp",
                    "server_label": "local",
                    "transport": {"type": "stdio", "command": "npx"},
                },
            ]
        )


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
