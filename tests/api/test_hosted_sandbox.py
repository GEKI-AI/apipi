import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from apipi.config import Settings
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


class _Proc:
    def __init__(self) -> None:
        self.alive = True
        self.image = None
        self.vm_id = None

    async def terminate(self) -> None:
        self.alive = False


async def test_hosted_environment_get_is_tenant_scoped(
    client: AsyncClient,
) -> None:
    token = "sandbox-get"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "openai_hosted"}},
    )
    assert created.status_code == 200
    body = created.json()
    env = body["environment"]
    assert env["status"] == "disconnected"
    assert env["sandbox"]["state"] == "none"
    assert env["sandbox"]["cold_boots"] == 0
    env_id = env["id"]
    uuid.UUID(env_id)
    assert "directory" not in env
    got = await client.get(f"/v1/agents/environments/{env_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == env_id
    assert got.json()["type"] == "openai_hosted"
    assert got.json()["status"] == "disconnected"
    assert got.json()["sandbox"]["state"] == "none"
    other = await client.get(
        f"/v1/agents/environments/{env_id}", headers=_auth("other-tenant")
    )
    assert other.status_code == 404


async def test_self_hosted_not_supported(client: AsyncClient) -> None:
    token = "sandbox-self"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "self_hosted"}},
    )
    assert created.status_code == 400
    assert created.json()["error"]["type"] == "not_implemented"
    assert "self_hosted" in created.json()["error"]["message"]


async def test_eager_boot_off_by_default(client: AsyncClient) -> None:
    token = "sandbox-lazy"
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id, "environment": {"type": "openai_hosted"}},
    )
    assert created.status_code == 200
    await asyncio.sleep(0.05)
    listed = await client.get(
        f"/v1/agents/sessions/{created.json()['id']}/events", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert all(
        event["type"] != "agent.session.environment.pending"
        for event in listed.json()["data"]
    )


async def test_eager_boot_override_starts_computer(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    async def fake_spawn(*_args: object, **_kwargs: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    from tests.support.split_worker import split_client_for

    async with split_client_for(settings, store, token=worker_secret) as (
        app,
        client,
        _worker,
    ):
        token = "sandbox-eager"
        agent_id = await _agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "openai_hosted"},
                "metadata": {"apipi.sandbox_eager_boot": True},
            },
        )
        assert created.status_code == 200
        tasks = list(app.state.gateway.sessions._turn_tasks)
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=5)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        session_id = created.json()["id"]
        for _ in range(50):
            got = await client.get(
                f"/v1/agents/sessions/{session_id}", headers=_auth(token)
            )
            if got.json()["environment"]["status"] == "connected":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(got.text)
        sandbox = got.json()["environment"]["sandbox"]
        assert sandbox["state"] == "ready"
        assert sandbox["cold_boots"] == 1
        assert got.json()["required_actions"] == []
