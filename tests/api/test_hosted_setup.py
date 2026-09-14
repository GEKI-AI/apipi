from pathlib import Path

import pytest
from httpx import AsyncClient

from apipi.env.setup import SetupError


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
    def fake_run(workspace: Path) -> None:
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
    def boom(_workspace: Path) -> None:
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
        ("files", []),
        ("env", {"A": "b"}),
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
