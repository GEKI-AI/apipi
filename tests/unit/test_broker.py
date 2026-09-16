import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from httpx import AsyncClient

from apipi.broker import DUMMY_KEY, start_broker
from apipi.config import Settings
from apipi.mcp.http import McpHttpServer, apply_vault_headers
from apipi.pi.proc import pi_env


def _settings(tmp_path: Path, base: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url=base,
        model_api_key_overwrite="real-model-key",
    )


async def test_broker_injects_model_key_and_strips_guest_auth(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["authorization"] = self.headers.get("Authorization", "")
            seen["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    settings = _settings(tmp_path, f"http://127.0.0.1:{port}/v1")
    broker = await start_broker(
        settings,
        api_key="from-request",
        mcp_http=None,
        host="127.0.0.1",
        port=0,
    )
    try:
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": "Bearer guest-dummy"},
                json={"model": "test"},
            )
        assert response.status_code == 200
        assert seen["authorization"] == "Bearer from-request"
        assert "chat/completions" in seen["path"]
    finally:
        await broker.stop()
        server.shutdown()


async def test_broker_injects_mcp_header(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class McpHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["authorization"] = self.headers.get("Authorization", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"jsonrpc":"2.0","id":1,"result":{}}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), McpHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    settings = _settings(tmp_path, "http://127.0.0.1/v1")
    mcp = [
        McpHttpServer(
            server_label="mock",
            server_url=f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": "Bearer vault-secret"},
        )
    ]
    broker = await start_broker(
        settings, api_key="k", mcp_http=mcp, host="127.0.0.1", port=0
    )
    try:
        async with AsyncClient() as client:
            response = await client.post(
                broker.mcp_url("0"),
                headers={"Authorization": "Bearer guest"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
        assert response.status_code == 200
        assert seen["authorization"] == "Bearer vault-secret"
    finally:
        await broker.stop()
        server.shutdown()


async def test_broker_unknown_mcp_is_404(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "http://127.0.0.1/v1")
    broker = await start_broker(
        settings, api_key="k", mcp_http=None, host="127.0.0.1", port=0
    )
    try:
        async with AsyncClient() as client:
            response = await client.post(broker.mcp_url("9"), json={})
        assert response.status_code == 404
    finally:
        await broker.stop()


def test_pi_env_with_broker_hides_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "https://api.openai.com/v1")

    class _Broker:
        openai_base_url = "http://127.0.0.1:9/tok/v1"

        def mcp_url(self, route_id: str) -> str:
            return f"http://127.0.0.1:9/tok/mcp/{route_id}"

    mcp = [
        McpHttpServer(
            server_label="tavily",
            server_url="https://mcp.tavily.com/mcp",
            headers={"Authorization": "Bearer secret"},
        )
    ]
    env = pi_env(settings, mcp, api_key="real-key", broker=_Broker())
    assert env["OPENAI_API_KEY"] == DUMMY_KEY
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9/tok/v1"
    assert "secret" not in env.values()
    assert env["APIPI_MCP_0_URL"] == "http://127.0.0.1:9/tok/mcp/0"
    assert "APIPI_MCP_0_AUTHORIZATION" not in env


def test_apply_vault_headers_match_and_conflict() -> None:
    class _Cred:
        def __init__(self, cred_id: str, url: str, token: str) -> None:
            self.id = cred_id
            self.mcp_server_url = url
            self.token = token

    servers = [
        McpHttpServer(
            server_label="a",
            server_url="https://mcp.example.com/mcp",
            headers={},
        )
    ]
    applied = apply_vault_headers(
        servers, [_Cred("c1", "https://mcp.example.com/mcp", "tok")]
    )
    assert applied[0].headers["Authorization"] == "Bearer tok"
    with pytest.raises(Exception, match="several vault"):
        apply_vault_headers(
            servers,
            [
                _Cred("c1", "https://mcp.example.com/mcp", "a"),
                _Cred("c2", "https://mcp.example.com/mcp", "b"),
            ],
        )
