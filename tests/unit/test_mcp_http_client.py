import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from httpx import AsyncClient

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.worker.pi.broker import start_broker


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url="http://127.0.0.1/v1",
    )


class _Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[dict[str, str]] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        msg = json.loads(raw.decode() or "{}")
        type(self).seen["authorization"] = self.headers.get("Authorization", "")
        type(self).seen["method"] = str(msg.get("method") or "")
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo"}]}
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            result = {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        else:
            self.send_response(400)
            self.end_headers()
            return
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


async def _rpc(
    client: AsyncClient, url: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        url,
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_http_client_calls_through_broker_without_bearer(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}
    handler = type("Handler", (_Upstream,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    port = server.server_address[1]
    secret = "Bearer vault-secret"
    settings = _settings(tmp_path)
    settings = settings.model_copy(update={"mcp_allow_hosts": "127.0.0.1"})
    broker = await start_broker(
        settings,
        api_key="k",
        mcp_http=[
            McpHttpServer(
                server_label="mock",
                server_url=f"http://127.0.0.1:{port}/mcp",
                headers={"Authorization": secret},
            )
        ],
        host="127.0.0.1",
        port=0,
    )
    try:
        url = broker.mcp_url("0")
        async with AsyncClient() as client:
            init = await _rpc(
                client,
                url,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            )
            assert init["result"]["serverInfo"]["name"] == "fixture"
            listed = await _rpc(client, url, "tools/list", {})
            assert [tool["name"] for tool in listed["result"]["tools"]] == ["echo"]
            called = await _rpc(
                client,
                url,
                "tools/call",
                {"name": "echo", "arguments": {"text": "hi"}},
            )
            assert called["result"]["content"] == [{"type": "text", "text": "hi"}]
        assert "vault-secret" not in url
        assert seen["authorization"] == secret
        assert seen["method"] == "tools/call"
    finally:
        await broker.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
