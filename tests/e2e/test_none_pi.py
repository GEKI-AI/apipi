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
from apipi.pi.artifacts import reap_workspaces
from apipi.pi.dirs import pi_session_file
from apipi.pi.pool import PiPool
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import get_session_by_id, get_session_turn
from apipi.tokens import hash_token

pytestmark = pytest.mark.e2e

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"
_MAPPED_PI_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 8,
    "cache_read_tokens": 1,
    "cache_write_tokens": 2,
    "total_tokens": 16,
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def none_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        idle_ttl=timedelta(milliseconds=250),
        pi_command=f"{sys.executable} {_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
    )


@pytest.fixture
def none_app(none_settings: Settings, store: Store) -> FastAPI:
    return create_app(none_settings, store=store)


@pytest.fixture
async def none_client(none_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=none_app),
        base_url="http://test",
    ) as client:
        yield client


async def test_none_openai_hosted_streams_fake_pi_text(
    none_client: AsyncClient, none_settings: Settings
) -> None:
    token = "e2e"
    created_agent = await none_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await none_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello-none"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    directory = Path(body["environment"]["directory"])
    assert directory.is_dir()
    root = Path(none_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    session_id = body["id"]
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-none"
    assert "assistantMessageEvent" not in done[0]["data"]


async def test_none_fake_pi_persists_usage(
    none_client: AsyncClient, store: Store
) -> None:
    token = "e2e-usage"
    created_agent = await none_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await none_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello-none",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    completed = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.completed"
    ]
    assert len(completed) == 1
    usage = completed[0]["data"]["usage"]
    assert usage == _MAPPED_PI_USAGE
    assert "cost" not in usage
    assert "prompt" not in usage
    assert "secret-prompt" not in str(usage)
    turn_id = completed[0]["data"]["turn_id"]
    one = await none_client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=_auth(token),
    )
    assert one.json()["usage"] == _MAPPED_PI_USAGE
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))
    async with store.session() as db:
        row = await get_session_turn(
            db, tenant_id, uuid.UUID(session_id), uuid.UUID(turn_id)
        )
    assert row is not None
    assert row.usage == _MAPPED_PI_USAGE


async def test_idle_ttl_kills_pi_session_stays(
    none_client: AsyncClient, none_app: FastAPI
) -> None:
    token = "ttl"
    created_agent = await none_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await none_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "openai_hosted"},
            "input": "stay",
        },
    )
    session_id = uuid.UUID(created.json()["id"])
    pool = none_app.state.pi_pool
    assert pool.alive(session_id)
    pool.settings.workspace_ttl = timedelta(seconds=0)
    await pool.reap()
    assert not pool.alive(session_id)
    got = await none_client.get(
        f"/v1/agents/sessions/{session_id}", headers=_auth(token)
    )
    assert got.status_code == 200
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert events.json()["data"]
    again = await none_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "resume"},
    )
    assert again.status_code == 200
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts[0] == "stay"
    assert "stay" in texts[1]
    assert "resume" in texts[1]


async def test_pi_session_restored_after_workspace_wipe(
    none_client: AsyncClient, none_app: FastAPI, none_settings: Settings, store: Store
) -> None:
    token = "restore"
    created_agent = await none_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await none_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "openai_hosted"},
            "input": "stay",
        },
    )
    assert created.status_code == 200
    session_id = uuid.UUID(created.json()["id"])
    directory = Path(created.json()["environment"]["directory"])
    (directory / "scratch.txt").write_text("gone", encoding="utf-8")
    pool = none_app.state.pi_pool
    await pool.kill(session_id)
    async with store.session() as db:
        row = await get_session_by_id(db, session_id)
        assert row is not None
        assert row.pi_session_id is not None
        row.updated_at = utc_now() - timedelta(hours=2)
    await reap_workspaces(none_settings, store, PiPool(none_settings))
    assert not directory.exists()
    again = await none_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "resume"},
    )
    assert again.status_code == 200
    assert directory.is_dir()
    assert not (directory / "scratch.txt").exists()
    assert pi_session_file(directory).is_file()
    events = await none_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    texts = [
        event["data"]["text"]
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert texts[0] == "stay"
    assert "stay" in texts[1]
    assert "resume" in texts[1]
