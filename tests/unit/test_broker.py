import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from httpx import AsyncClient

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer, apply_vault_headers
from apipi.services.sessions import _plain_vault_creds
from apipi.services.vault_crypto import encrypt_vault_token, vault_aad, vault_key_bytes
from apipi.worker.pi.broker import DUMMY_KEY, start_broker
from apipi.worker.pi.proc import pi_env


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


async def test_broker_stamps_attribution_and_strips_forged(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            for name in (
                "x-apipi-session-id",
                "x-apipi-turn-id",
                "x-apipi-agent-id",
            ):
                seen[name] = self.headers.get(name, "")
            seen["authorization"] = self.headers.get("Authorization", "")
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
        broker.set_context("sess-1", "agent-9")
        broker.set_turn("turn-7")
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            response = await client.post(
                "/chat/completions",
                headers={
                    "Authorization": "Bearer guest-dummy",
                    "x-apipi-session-id": "forged",
                    "X-Apipi-Turn-Id": "forged",
                },
                json={"model": "test"},
            )
        assert response.status_code == 200
        assert seen["x-apipi-session-id"] == "sess-1"
        assert seen["x-apipi-turn-id"] == "turn-7"
        assert seen["x-apipi-agent-id"] == "agent-9"
        assert seen["authorization"] == "Bearer from-request"
        broker.clear_turn()
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            await client.post("/chat/completions", json={"model": "test"})
        assert seen["x-apipi-turn-id"] == ""
        assert seen["x-apipi-session-id"] == "sess-1"
    finally:
        await broker.stop()
        server.shutdown()


async def test_broker_attribution_toggle_still_strips(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["session"] = self.headers.get("x-apipi-session-id", "")
            seen["other"] = self.headers.get("x-other", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    settings = _settings(tmp_path, f"http://127.0.0.1:{port}/v1")
    broker = await start_broker(
        settings,
        api_key="k",
        mcp_http=None,
        host="127.0.0.1",
        port=0,
    )
    try:
        broker.attribution = False
        broker.set_context("sess-1", "agent-1")
        broker.set_turn("turn-1")
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            await client.post(
                "/chat/completions",
                headers={"x-apipi-session-id": "forged", "x-other": "kept"},
                json={},
            )
        assert seen["session"] == ""
        assert seen["other"] == "kept"
    finally:
        await broker.stop()
        server.shutdown()


async def test_broker_omits_agent_headers_for_inline(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["agent"] = self.headers.get("x-apipi-agent-id", "")
            seen["turn"] = self.headers.get("x-apipi-turn-id", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    settings = _settings(tmp_path, f"http://127.0.0.1:{port}/v1")
    broker = await start_broker(
        settings, api_key="k", mcp_http=None, host="127.0.0.1", port=0
    )
    try:
        broker.set_context("sess-1", None)
        broker.set_turn("turn-1")
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            await client.post("/chat/completions", json={})
        assert seen["turn"] == "turn-1"
        assert seen["agent"] == ""
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
    settings = settings.model_copy(update={"mcp_allow_hosts": "127.0.0.1"})
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
            allowed_tools=("search",),
        )
    ]
    env = pi_env(settings, mcp, api_key="real-key", broker=_Broker())
    assert env["OPENAI_API_KEY"] == DUMMY_KEY
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9/tok/v1"
    assert "secret" not in env.values()
    assert env["APIPI_MCP_0_URL"] == "http://127.0.0.1:9/tok/mcp/0"
    assert "APIPI_MCP_0_AUTHORIZATION" not in env
    assert env["APIPI_MCP_0_ALLOWED"] == "search"


def test_plain_vault_creds_decrypt_for_broker() -> None:
    class _Cred:
        def __init__(self) -> None:
            self.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
            self.tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
            self.auth_type = "static_bearer"
            self.mcp_server_url = "https://mcp.example.com/mcp"
            self.token = encrypt_vault_token(
                "tok",
                vault_key_bytes(None),
                aad=vault_aad(self.tenant_id, self.id),
            )

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
    )
    servers = [
        McpHttpServer(
            server_label="a",
            server_url="https://mcp.example.com/mcp",
            headers={},
        )
    ]
    applied = apply_vault_headers(servers, _plain_vault_creds(settings, [_Cred()]))
    assert applied[0].headers["Authorization"] == "Bearer tok"


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


async def test_broker_rejects_private_mcp_host_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "http://127.0.0.1/v1")
    mcp = [
        McpHttpServer(
            server_label="mock",
            server_url="http://127.0.0.1:9/mcp",
            headers={},
        )
    ]
    with pytest.raises(Exception, match="blocked host"):
        await start_broker(
            settings, api_key="k", mcp_http=mcp, host="127.0.0.1", port=0
        )


async def test_broker_allowlists_private_mcp_host(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class McpHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen["hit"] = "yes"
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
    settings = settings.model_copy(update={"mcp_allow_hosts": "127.0.0.1"})
    mcp = [
        McpHttpServer(
            server_label="mock",
            server_url=f"http://127.0.0.1:{port}/mcp",
            headers={},
        )
    ]
    broker = await start_broker(
        settings, api_key="k", mcp_http=mcp, host="127.0.0.1", port=0
    )
    try:
        async with AsyncClient() as client:
            response = await client.post(broker.mcp_url("0"), json={})
        assert response.status_code == 200
        assert seen.get("hit") == "yes"
    finally:
        await broker.stop()
        server.shutdown()


async def test_pi_harness_applies_the_model_key_every_turn(tmp_path: Path) -> None:
    from typing import Any

    from apipi.worker.pi.harness import PiHarness

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = _settings(tmp_path, f"http://127.0.0.1:{server.server_address[1]}/v1")
    settings = settings.model_copy(update={"model_api_key_overwrite": None})
    broker = await start_broker(
        settings, api_key="first", mcp_http=None, host="127.0.0.1", port=0
    )

    class _Proc:
        def __init__(self) -> None:
            self.broker = broker

        async def prompt(self, _text: str, **_kwargs: object) -> Any:
            async with AsyncClient(base_url=broker.openai_base_url) as client:
                await client.post("/chat/completions", json={"model": "m"})
            yield {"type": "agent_settled", "success": True}

    proc = _Proc()

    class _Pool:
        spawned = 0

        async def get(self, *args: object, **kwargs: object) -> Any:
            return proc

        def touch(self, session_id: object) -> None:
            del session_id

    harness = PiHarness(_Pool())  # ty: ignore[invalid-argument-type]
    session_id = uuid.uuid4()
    try:
        for key in ("first", "rotated"):
            [
                event
                async for event in harness.generate(
                    "hi", session_id=session_id, api_key=key
                )
            ]
        assert seen == ["Bearer first", "Bearer rotated"]
    finally:
        await broker.stop()
        server.shutdown()
