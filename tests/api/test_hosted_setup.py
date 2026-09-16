import base64
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.env.setup import SetupError
from apipi.pi.artifacts import reap_workspaces
from apipi.pi.isolation.none import NoneIsolation
from apipi.pi.pool import PiPool
from apipi.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import get_session_by_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def test_packages_and_setup_commands_are_stored(client: AsyncClient) -> None:
    token = "setup-store"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "packages": {"python": ["pandas==2.2.3"], "npm": ["typescript"]},
                "setup_commands": [{"command": "mkdir -p reports"}],
            },
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["packages"] == {"python": ["pandas==2.2.3"], "npm": ["typescript"]}
    assert env["setup_commands"] == [{"command": "mkdir -p reports"}]
    script = Path(env["directory"]) / ".apipi" / "setup.sh"
    assert script.is_file()
    text = script.read_text()
    assert "pandas==2.2.3" in text
    assert "typescript" in text
    assert "mkdir -p reports" in text


async def test_setup_runs_before_turn(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(workspace: Path, **_kwargs: object) -> None:
        (workspace / "ready.txt").write_text("ok")

    monkeypatch.setattr("apipi.env.setup.run_host_setup", fake_run)
    token = "setup-run"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "packages": {"python": ["rich"]},
            },
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "idle"
    ready = Path(created.json()["environment"]["directory"]) / "ready.txt"
    assert ready.read_text() == "ok"
    events = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.completed" in types
    assert "agent.session.environment.failed" not in types


async def test_setup_failure_does_not_start_turn(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_workspace: Path, **_kwargs: object) -> None:
        raise SetupError("pip failed")

    monkeypatch.setattr("apipi.env.setup.run_host_setup", boom)
    token = "setup-fail"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "packages": {"python": ["rich"]},
            },
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "failed"
    events = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}/events", headers=_auth(token)
    )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.environment.failed" in types
    assert "agent.session.failed" in types
    assert "agent.session.turn.created" not in types
    failed = next(
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.environment.failed"
    )
    assert failed["data"]["error"] == "pip failed"


async def test_packages_rejected_on_none(client: AsyncClient) -> None:
    token = "setup-none"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "packages": {"python": ["rich"]}},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_unimplemented_env_fields(client: AsyncClient) -> None:
    token = "setup-unimpl"
    agent_id = await _agent(client, token)
    for field, value in (
        ("network", {"access": "disabled"}),
        ("environment_template_id", "tpl"),
        ("skills", []),
        ("plugins", []),
    ):
        response = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "openai_hosted", field: value},
            },
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["type"] == "not_implemented"
        assert error["code"] == field


async def test_sandbox_ttl_wipes_scratch_and_rehydrates(
    client: AsyncClient,
    store: Store,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(workspace: Path, **_kwargs: object) -> None:
        (workspace / "ready.txt").write_text("ok")

    monkeypatch.setattr("apipi.env.setup.run_host_setup", fake_run)
    token = "sandbox-ttl"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "setup_commands": [{"command": "mkdir -p reports"}],
            },
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    directory = Path(created.json()["environment"]["directory"])
    (directory / "scratch.txt").write_text("gone")
    async with store.session() as db:
        row = await get_session_by_id(db, UUID(session_id))
        assert row is not None
        row.updated_at = utc_now() - timedelta(hours=2)
    await reap_workspaces(settings, store, PiPool(settings))
    assert not directory.exists()
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "text": "hello"},
    )
    assert follow.status_code == 200
    assert not (directory / "scratch.txt").exists()
    assert (directory / "ready.txt").read_text() == "ok"
    assert (directory / ".apipi" / "setup.sh").is_file()


async def test_reap_skips_held_hosted_workspace(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = "reap-held"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "openai_hosted"},
        },
    )
    assert created.status_code == 200
    session_id = UUID(created.json()["id"])
    directory = Path(created.json()["environment"]["directory"])
    (directory / "scratch.txt").write_text("keep", encoding="utf-8")
    async with store.session() as db:
        row = await get_session_by_id(db, session_id)
        assert row is not None
        row.updated_at = utc_now() - timedelta(hours=2)
    pool = PiPool(settings)
    pool.hold(session_id)
    await reap_workspaces(settings, store, pool)
    assert directory.is_dir()
    assert (directory / "scratch.txt").read_text(encoding="utf-8") == "keep"
    pool.release(session_id)
    await reap_workspaces(settings, store, pool)
    assert not directory.exists()


async def test_none_spawn_creates_missing_cwd(tmp_path: Path) -> None:
    missing = tmp_path / "tenant" / "session"
    assert not missing.exists()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        pi_command="true",
        sessions_dir=str(tmp_path / "sessions"),
    )
    proc = await NoneIsolation().spawn(settings, cwd=str(missing), tools=False)
    assert missing.is_dir()
    await proc.terminate()


class _SpawnFailHarness(FakeHarness):
    async def generate(self, text: str, **kwargs: object):  # type: ignore[override]
        del text, kwargs
        raise FileNotFoundError("No such file or directory")
        yield  # pragma: no cover


async def test_spawn_oserror_fails_turn_not_500(
    settings: Settings, store: Store
) -> None:
    app = create_app(settings, store=store, harness=_SpawnFailHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "spawn-fail"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "openai_hosted"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert created.json()["status"] == "idle"
        session_id = created.json()["id"]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        body = events.json()["data"]
        types = [event["type"] for event in body]
        assert "agent.session.turn.failed" in types
        assert "agent.session.error" in types
        error = next(event for event in body if event["type"] == "agent.session.error")
        assert error["data"]["code"] == "spawn_failed"


async def test_env_and_inline_files_are_stored(client: AsyncClient) -> None:
    token = "env-files"
    agent_id = await _agent(client, token)
    payload = base64.b64encode(b"a,b\n1,2\n").decode()
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "env": {"REPORT": "yes"},
                "files": [
                    {
                        "type": "inline",
                        "path": "/workspace/amounts.csv",
                        "data": payload,
                    }
                ],
            },
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["env"] == {"REPORT": "yes"}
    assert env["files"][0]["path"] == "/workspace/amounts.csv"
    directory = Path(env["directory"])
    assert (directory / "amounts.csv").read_bytes() == b"a,b\n1,2\n"
    user_env = (directory / ".apipi" / "user.env").read_text()
    assert "REPORT=" in user_env


async def test_reserved_env_name_rejected(client: AsyncClient) -> None:
    token = "env-reserved"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "env": {"OPENAI_API_KEY": "nope"},
            },
        },
    )
    assert response.status_code == 400
    assert "reserved env" in response.json()["error"]["message"]


async def test_env_rejected_on_none(client: AsyncClient) -> None:
    token = "env-none"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none", "env": {"A": "b"}},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_non_inline_files_not_implemented(client: AsyncClient) -> None:
    token = "files-id"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [{"type": "file", "file_id": "file_123"}],
            },
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "not_implemented"
    assert error["code"] == "files"
