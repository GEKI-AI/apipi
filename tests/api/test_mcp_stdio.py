import sys
from collections.abc import AsyncIterator
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store

_STDIO = Path(__file__).resolve().parents[1] / "support" / "mcp_stdio.py"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _client(
    settings: Settings, store: Store, harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_mcp_stdio_starts_with_session(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness):
        created_agent = await client.post(
            "/v1/agents",
            headers=_auth("stdio"),
            json={
                "name": "bot",
                "model": "test",
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "local",
                        "transport": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [str(_STDIO)],
                        },
                    }
                ],
            },
        )
        assert created_agent.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("stdio"),
            json={
                "agent_id": created_agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert created.json()["status"] == "idle"
        assert harness.mcp_stdio is not None
        assert harness.mcp_stdio[0].server_label == "local"
        assert harness.mcp_stdio[0].process.returncode is None
        session_id = created.json()["id"]
        deleted = await client.delete(
            f"/v1/agents/sessions/{session_id}", headers=_auth("stdio")
        )
        assert deleted.status_code == 200
        assert harness.mcp_stdio[0].process.returncode is not None


async def test_mcp_stdio_failure_is_explicit(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness):
        created_agent = await client.post(
            "/v1/agents",
            headers=_auth("stdio"),
            json={
                "name": "bot",
                "model": "test",
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "broken",
                        "transport": {
                            "type": "stdio",
                            "command": "mcp-stdio-does-not-exist",
                            "args": [],
                        },
                    }
                ],
            },
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("stdio"),
            json={
                "agent_id": created_agent.json()["id"],
                "environment": {"type": "none"},
            },
        )
        assert created.status_code == 200
        assert created.json()["status"] == "failed"
        session_id = created.json()["id"]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth("stdio")
        )
        types = [event["type"] for event in events.json()["data"]]
        assert "agent.session.error" in types
        assert types[-1] == "agent.session.failed"
        other = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth("other")
        )
        assert other.status_code == 404
