import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest


class _Handler(BaseHTTPRequestHandler):
    status = 200
    seen: ClassVar[dict[str, str]] = {}

    def do_POST(self) -> None:
        auth = self.headers.get("Authorization")
        if auth is not None:
            type(self).seen["Authorization"] = auth
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.status >= 400:
            self.send_response(self.status)
            self.end_headers()
            return
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "mock"},
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(status: int) -> Iterator[tuple[str, dict[str, str]]]:
    seen: dict[str, str] = {}
    handler = type("Handler", (_Handler,), {"status": status, "seen": seen})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/mcp", seen
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def mcp_server() -> Iterator[tuple[str, dict[str, str]]]:
    yield from _serve(200)


@pytest.fixture
def mcp_url(mcp_server: tuple[str, dict[str, str]]) -> str:
    return mcp_server[0]


@pytest.fixture
def mcp_fail() -> Iterator[tuple[str, dict[str, str]]]:
    yield from _serve(500)


@pytest.fixture
def mcp_fail_url(mcp_fail: tuple[str, dict[str, str]]) -> str:
    return mcp_fail[0]
