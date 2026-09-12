import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.store.engine import Store

pytestmark = pytest.mark.e2e

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def host_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
        idle_ttl=timedelta(milliseconds=250),
        pi_command=f"{sys.executable} {_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
    )


@pytest.fixture
def host_app(host_settings: Settings, store: Store) -> FastAPI:
    return create_app(host_settings, store=store)


@pytest.fixture
async def host_client(host_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=host_app),
        base_url="http://test",
    ) as client:
        yield client


async def test_host_openai_hosted_streams_fake_pi_text(
    host_client: AsyncClient, host_settings: Settings
) -> None:
    token = "e2e"
    created_agent = await host_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await host_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello-host"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    directory = Path(body["environment"]["directory"])
    assert directory.is_dir()
    root = Path(host_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    session_id = body["id"]
    events = await host_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-host"
    assert "assistantMessageEvent" not in done[0]["data"]


async def test_idle_ttl_kills_pi_session_stays(
    host_client: AsyncClient, host_app: FastAPI
) -> None:
    token = "ttl"
    created_agent = await host_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await host_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "openai_hosted"},
            "input": "stay",
        },
    )
    session_id = uuid.UUID(created.json()["id"])
    pool = host_app.state.pi_pool
    assert pool.alive(session_id)
    pool.settings.idle_ttl = timedelta(seconds=0)
    await pool.reap()
    assert not pool.alive(session_id)
    got = await host_client.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth(token)
    )
    assert got.status_code == 200
    events = await host_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert events.json()["data"]
    again = await host_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "resume"},
    )
    assert again.status_code == 200
    events = await host_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts == ["stay", "resume"]
