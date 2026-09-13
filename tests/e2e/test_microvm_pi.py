import shutil
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import ConfigError, Settings
from apipi.pi.microvm import require_microvm
from apipi.store.engine import Store

pytestmark = [pytest.mark.e2e, pytest.mark.microvm]

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"
_GUEST_FAKE_PI = "/tmp/workspace/fake_pi.py"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _microvm_or_skip(settings: Settings) -> None:
    try:
        require_microvm(settings)
    except ConfigError as exc:
        pytest.skip(str(exc))


@pytest.fixture
def microvm_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        idle_ttl=timedelta(seconds=30),
        pi_command=f"python3 {_GUEST_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
    )
    _microvm_or_skip(settings)
    return settings


@pytest.fixture
def microvm_app(microvm_settings: Settings, store: Store) -> FastAPI:
    return create_app(microvm_settings, store=store)


@pytest.fixture
async def microvm_client(microvm_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=microvm_app),
        base_url="http://test",
        timeout=60,
    ) as client:
        yield client


async def test_microvm_openai_hosted_streams_fake_pi_text(
    microvm_client: AsyncClient, microvm_settings: Settings
) -> None:
    token = "e2e-microvm"
    created_agent = await microvm_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await microvm_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    directory = Path(body["environment"]["directory"])
    assert directory.is_dir()
    root = Path(microvm_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    session_id = body["id"]
    turned = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "hello-microvm"},
    )
    assert turned.status_code == 200
    events = await microvm_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-microvm"
