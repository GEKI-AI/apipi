import json
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import ConfigError, Settings
from apipi.pi.jail import jail_argv, jail_binaries, require_jail, resolv_conf
from apipi.store.engine import Store

pytestmark = pytest.mark.e2e

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _jail_or_skip() -> None:
    try:
        require_jail()
    except ConfigError as exc:
        pytest.skip(str(exc))


@pytest.fixture
def jail_settings(tmp_path: Path) -> Settings:
    _jail_or_skip()
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="jail",
        idle_ttl=timedelta(milliseconds=250),
        pi_command=f"{sys.executable} {_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
    )


@pytest.fixture
def jail_app(jail_settings: Settings, store: Store) -> FastAPI:
    return create_app(jail_settings, store=store)


@pytest.fixture
async def jail_client(jail_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=jail_app),
        base_url="http://test",
    ) as client:
        yield client


async def test_jail_openai_hosted_streams_fake_pi_text(
    jail_client: AsyncClient, jail_settings: Settings
) -> None:
    token = "e2e-jail"
    created_agent = await jail_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await jail_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "input": "hello-jail"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    directory = Path(body["environment"]["directory"])
    assert directory.is_dir()
    root = Path(jail_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    session_id = body["id"]
    events = await jail_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-jail"


def test_jail_hides_sibling_session_dir(tmp_path: Path) -> None:
    _jail_or_skip()
    bwrap, pasta = jail_binaries()
    sessions = tmp_path / "sessions"
    cwd = sessions / "tenant" / "session"
    sibling = sessions / "tenant" / "other" / "secret.txt"
    visible = cwd / "note.txt"
    cwd.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    visible.write_text("ok", encoding="utf-8")
    sibling.write_text("no", encoding="utf-8")
    probe = (
        "import json, os\n"
        f"print(json.dumps({{'visible': os.path.exists({str(visible)!r}), "
        f"'hidden': os.path.exists({str(sibling)!r})}}))\n"
    )
    argv = jail_argv(
        [sys.executable, "-c", probe],
        cwd=str(cwd),
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)},
        bwrap=bwrap,
        pasta=pasta,
        resolv=str(resolv_conf()),
        sessions_dir=str(sessions),
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"visible": True, "hidden": False}
