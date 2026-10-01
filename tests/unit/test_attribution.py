import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from httpx import AsyncClient

from apipi.config import Settings
from apipi.worker.pi.broker import start_broker


def _settings(tmp_path: Path, base: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url=base,
        model_api_key_overwrite="real-model-key",
    )


async def test_consecutive_turns_change_turn_id(store, tmp_path: Path) -> None:
    turns: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            turns.append(self.headers.get("x-apipi-turn-id", ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    broker = await start_broker(
        _settings(tmp_path, f"http://127.0.0.1:{port}/v1"),
        api_key="k",
        mcp_http=None,
        host="127.0.0.1",
        port=0,
    )
    try:
        broker.set_context("sess-1", None)
        async with AsyncClient(base_url=broker.openai_base_url) as client:
            broker.set_turn("turn-1")
            await client.post("/chat/completions", json={})
            broker.set_turn("turn-2")
            await client.post("/chat/completions", json={})
            broker.clear_turn()
            await client.post("/chat/completions", json={})
        assert turns == ["turn-1", "turn-2", ""]
    finally:
        await broker.stop()
        server.shutdown()
