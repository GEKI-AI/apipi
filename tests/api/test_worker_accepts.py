import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )


async def _agent(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    return str(agent.json()["id"])


@pytest.mark.parametrize(
    ("kind", "pools", "want"),
    [
        ("none", "none", "none"),
        ("none", "microvm", None),
        ("none", "both", "none"),
        ("computer", "none", None),
        ("computer", "microvm", "microvm"),
        ("computer", "both", "microvm"),
    ],
)
async def test_placement_matrix(
    settings: Settings,
    store: Store,
    worker_secret: str,
    kind: str,
    pools: str,
    want: str | None,
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = f"matrix-{kind}-{pools}"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, token)
        if kind == "computer":
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={
                    "agent_id": agent_id,
                    "environment": {"type": "openai_hosted"},
                },
            )
        else:
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={"agent_id": agent_id, "environment": {"type": "none"}},
            )
        assert created.status_code == 200
        session_id = uuid.UUID(created.json()["id"])
        workers: list[FakeWorker] = []
        if pools in {"none", "both"}:
            none_worker = FakeWorker(app, worker_secret)
            await none_worker.connect(capacity=4, run_mode="none", accepts=["none"])
            workers.append(none_worker)
        if pools in {"microvm", "both"}:
            microvm_secret = (await create_token(store, name="microvm")).secret
            microvm = FakeWorker(app, microvm_secret)
            if pools == "both":
                await microvm.connect(
                    capacity=4, run_mode="microvm", accepts=["none", "microvm"]
                )
            else:
                await microvm.connect(
                    capacity=4, run_mode="microvm", accepts=["microvm"]
                )
            workers.append(microvm)
        command = await app.state.workers.acquire(
            store, tenant_id, session_id, op="turn.start"
        )
        if want is None:
            assert command is None
        else:
            assert command is not None
            assert command["payload"]["run_mode"] == want
            holder = None
            for worker in workers:
                live = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
                if live is not None and uuid.UUID(command["lease_id"]) in live.leases:
                    holder = live
            assert holder is not None
            assert want in holder.accepts
        for worker in workers:
            await worker.close()


async def test_type_none_rejects_bash_tools(client: AsyncClient) -> None:
    token = "deny"
    agent_id = await _agent(client, token)
    bash = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "agent": {
                "name": "bot",
                "model": "test",
                "tools": [{"type": "bash", "name": "bash"}],
            },
        },
    )
    assert bash.status_code == 400


async def test_type_none_rejects_non_http_mcp(client: AsyncClient) -> None:
    token = "deny-mcp"
    stdio = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "environment": {"type": "none"},
            "agent": {
                "name": "bot",
                "model": "test",
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "shell",
                        "transport": {"type": "stdio", "command": "bash"},
                    }
                ],
            },
        },
    )
    assert stdio.status_code == 400


async def test_type_none_allows_function_and_http_mcp(client: AsyncClient) -> None:
    token = "allow"
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "environment": {"type": "none"},
            "agent": {
                "name": "bot",
                "model": "test",
                "tools": [
                    {"type": "function", "name": "echo"},
                ],
            },
            "input": "hello",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["environment"]["type"] == "none"


async def test_agent_with_none_defaults_rejects_bad_tools(
    client: AsyncClient,
) -> None:
    token = "agent-none"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "tools": [{"type": "bash", "name": "bash"}],
            "session_defaults": {"environment": {"type": "none"}},
        },
    )
    assert created.status_code == 400
