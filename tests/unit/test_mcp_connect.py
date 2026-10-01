import pytest

from apipi.mcp.guard import (
    check_mcp_url_sync,
    split_allow_hosts,
)
from apipi.mcp.http import (
    McpConnectError,
    McpHttpServer,
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
                "headers": {"Authorization": "Bearer secret"},
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


def test_mcp_http_tools_rejects_env_reference_in_headers() -> None:
    with pytest.raises(McpConnectError, match="must not contain"):
        mcp_http_tools(
            [
                {
                    "type": "mcp",
                    "server_label": "tavily",
                    "server_url": "https://mcp.tavily.com/mcp",
                    "headers": {"Authorization": "Bearer ${TAVILY_API_KEY}"},
                },
            ]
        )


def test_mcp_http_tools_rejects_partial_env_reference() -> None:
    with pytest.raises(McpConnectError, match="vault"):
        mcp_http_tools(
            [
                {
                    "type": "mcp",
                    "server_label": "docs",
                    "server_url": "https://mcp.example.com/mcp",
                    "headers": {"X-Api-Key": "prefix-${DOCS_KEY}"},
                },
            ]
        )


def test_mcp_http_server_is_frozen() -> None:
    server = McpHttpServer(server_label="x", server_url="http://x", headers={})
    assert server.server_label == "x"


def test_split_allow_hosts() -> None:
    assert split_allow_hosts("") == ()
    assert split_allow_hosts(None) == ()
    assert split_allow_hosts("127.0.0.1, 10.0.0.0/8") == ("127.0.0.1", "10.0.0.0/8")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/mcp",
        "http://127.0.0.1:8000/mcp",
        "http://10.0.0.1/mcp",
        "http://172.16.5.4/mcp",
        "http://192.168.1.10/mcp",
        "http://169.254.169.254/mcp",
        "http://100.64.0.1/mcp",
        "http://100.100.100.200/mcp",
        "http://[::1]/mcp",
        "http://[fe80::1]/mcp",
        "http://[fc00::1]/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "http://metadata.google.internal/mcp",
        "ftp://example.com/mcp",
        "not-a-url",
    ],
)
def test_guard_blocks_private_targets_by_default(url: str) -> None:
    with pytest.raises(McpConnectError, match="blocked host"):
        check_mcp_url_sync(url, label="mock")


def test_guard_blocks_hostname_resolving_to_private_ip() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "mcp.internal.example"
        return ["10.9.8.7"]

    with pytest.raises(McpConnectError, match="blocked host"):
        check_mcp_url_sync(
            "https://mcp.internal.example/mcp", label="mock", resolve=fake_resolve
        )


def test_guard_blocks_redirect_target_with_private_ip() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "redirect.example"
        return ["192.168.4.4"]

    with pytest.raises(McpConnectError, match="blocked host"):
        check_mcp_url_sync(
            "https://redirect.example/mcp", label="mock", resolve=fake_resolve
        )


def test_guard_allows_public_ip() -> None:
    check_mcp_url_sync("https://8.8.8.8/mcp", label="mock")


def test_guard_allows_public_hostname() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "mcp.example.com"
        return ["93.184.216.34"]

    check_mcp_url_sync(
        "https://mcp.example.com/mcp", label="mock", resolve=fake_resolve
    )


def test_guard_allowlist_admits_listed_private_host() -> None:
    check_mcp_url_sync(
        "http://127.0.0.1:8000/mcp", label="mock", allow_hosts=("127.0.0.1",)
    )
    check_mcp_url_sync("http://10.9.8.7/mcp", label="mock", allow_hosts=("10.0.0.0/8",))


def test_guard_allowlist_admits_listed_hostname() -> None:
    def fake_resolve(host: str) -> list[str]:
        assert host == "mcp.internal.example"
        return ["10.9.8.7"]

    check_mcp_url_sync(
        "https://mcp.internal.example/mcp",
        label="mock",
        allow_hosts=("mcp.internal.example",),
        resolve=fake_resolve,
    )


def test_guard_allowlist_does_not_admit_others() -> None:
    with pytest.raises(McpConnectError, match="blocked host"):
        check_mcp_url_sync(
            "http://192.168.1.10/mcp", label="mock", allow_hosts=("127.0.0.1",)
        )
