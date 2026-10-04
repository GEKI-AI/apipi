import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.common.errors import ApiError
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.protocol import TurnContext, redact_context
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.worker.turn_context import (
    env_credential_hosts,
    env_credentials_from_context,
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid5(NAMESPACE_URL, hash_token(token))


def _settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "store"),
        **extra,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path)


async def _client(settings: Settings, store: Store) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def client(settings: Settings, store: Store) -> AsyncIterator[AsyncClient]:
    async for item in _client(settings, store):
        yield item


async def _agent(
    client: AsyncClient, token: str, defaults: dict[str, Any] | None = None
) -> str:
    body: dict[str, Any] = {"name": "bot", "model": "test"}
    if defaults is not None:
        body["session_defaults"] = defaults
    response = await client.post("/v1/agents", headers=_auth(token), json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _vault(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "v"}
    )
    return str(response.json()["id"])


async def _env_cred(
    client: AsyncClient,
    token: str,
    vault_id: str,
    *,
    secret_name: str = "GITHUB_TOKEN",
    secret_value: str = "ghp_secret",
    hosts: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    body: dict[str, Any] = {
        "auth": {
            "type": "environment_variable",
            "secret_name": secret_name,
            "secret_value": secret_value,
            "networking": {
                "type": "limited",
                "allowed_hosts": hosts or ["github.com", "api.github.com"],
            },
        }
    }
    if metadata is not None:
        body["metadata"] = metadata
    response = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials", headers=_auth(token), json=body
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _session(
    client: AsyncClient,
    token: str,
    agent_id: str,
    vault_ids: list[str] | None = None,
    environment: dict[str, Any] | None = None,
) -> Any:
    body: dict[str, Any] = {"agent_id": agent_id}
    if vault_ids is not None:
        body["vault_ids"] = vault_ids
    if environment is not None:
        body["environment"] = environment
    return await client.post("/v1/agents/sessions", headers=_auth(token), json=body)


def _code(response: Any) -> str:
    assert response.status_code == 400, response.text
    return str(response.json()["error"]["code"])


async def test_env_credentials_need_a_microvm_session(client: AsyncClient) -> None:
    token = "env-none"
    agent_id = await _agent(client, token)
    vault_id = await _vault(client, token)
    await _env_cred(client, token, vault_id)
    response = await _session(client, token, agent_id, [vault_id], {"type": "none"})
    assert _code(response) == "credential_not_allowed"
    assert "microVM" in response.json()["error"]["message"]
    hosted = await _session(
        client, token, agent_id, [vault_id], {"type": "openai_hosted"}
    )
    assert hosted.status_code == 200


async def test_mcp_only_vault_still_works_on_none(client: AsyncClient) -> None:
    token = "env-none-mcp"
    agent_id = await _agent(client, token)
    vault_id = await _vault(client, token)
    created = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "t",
            }
        },
    )
    assert created.status_code == 200
    response = await _session(client, token, agent_id, [vault_id], {"type": "none"})
    assert response.status_code == 200


async def test_env_credentials_need_network_access(client: AsyncClient) -> None:
    token = "env-disabled"
    agent_id = await _agent(client, token)
    vault_id = await _vault(client, token)
    await _env_cred(client, token, vault_id)
    response = await _session(
        client,
        token,
        agent_id,
        [vault_id],
        {"type": "openai_hosted", "network": {"access": "disabled"}},
    )
    assert _code(response) == "credential_not_allowed"
    assert "disabled" in response.json()["error"]["message"]


async def test_duplicate_secret_name_across_vaults(client: AsyncClient) -> None:
    token = "env-dup"
    agent_id = await _agent(client, token)
    first = await _vault(client, token)
    second = await _vault(client, token)
    await _env_cred(client, token, first)
    await _env_cred(client, token, second)
    response = await _session(client, token, agent_id, [first, second])
    assert _code(response) == "secret_name_collision"


async def test_secret_name_clashes_with_environment_env(client: AsyncClient) -> None:
    token = "env-clash"
    agent_id = await _agent(client, token)
    vault_id = await _vault(client, token)
    await _env_cred(client, token, vault_id)
    response = await _session(
        client,
        token,
        agent_id,
        [vault_id],
        {"type": "openai_hosted", "env": {"GITHUB_TOKEN": "plain"}},
    )
    assert _code(response) == "secret_name_collision"
    assert "environment.env" in response.json()["error"]["message"]


async def test_agent_session_defaults_are_checked(client: AsyncClient) -> None:
    token = "env-defaults"
    first = await _vault(client, token)
    await _env_cred(client, token, first)
    agent_none = await _agent(
        client, token, {"vault_ids": [first], "environment": {"type": "none"}}
    )
    response = await _session(client, token, agent_none)
    assert _code(response) == "credential_not_allowed"
    second = await _vault(client, token)
    await _env_cred(client, token, second)
    agent = await _agent(client, token, {"vault_ids": [first]})
    merged = await _session(client, token, agent, [second])
    assert _code(merged) == "secret_name_collision"
    skipped = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent,
            "vault_ids": [second],
            "inherit_agent_defaults": False,
        },
    )
    assert skipped.status_code == 200


async def test_operator_allowlist_must_allow_credential_hosts(
    tmp_path: Path, store: Store
) -> None:
    token = "env-allowlist"
    locked = _settings(
        tmp_path,
        microvm_egress_allowlist=True,
        microvm_egress_hosts="github.com",
    )
    async for client in _client(locked, store):
        agent_id = await _agent(client, token)
        vault_id = await _vault(client, token)
        await _env_cred(client, token, vault_id)
        response = await _session(client, token, agent_id, [vault_id])
        assert _code(response) == "credential_host_not_allowed"
        assert "api.github.com" in response.json()["error"]["message"]
    opened = _settings(
        tmp_path,
        microvm_egress_allowlist=True,
        microvm_egress_hosts="github.com,https://API.github.com",
    )
    async for client in _client(opened, store):
        agent_id = await _agent(client, token)
        vault_id = await _vault(client, token)
        await _env_cred(client, token, vault_id)
        response = await _session(client, token, agent_id, [vault_id])
        assert response.status_code == 200


async def test_context_carries_decrypted_env_credentials(
    settings: Settings, store: Store, client: AsyncClient
) -> None:
    token = "env-context"
    agent_id = await _agent(client, token)
    vault_id = await _vault(client, token)
    cred_id = await _env_cred(
        client,
        token,
        vault_id,
        secret_name="FORGEJO_TOKEN",
        secret_value="forgejo-secret",
        hosts=["git.example.com"],
        metadata={"apipi.git_username": "forgejo-bot"},
    )
    created = await _session(
        client,
        token,
        agent_id,
        [vault_id],
        {
            "type": "openai_hosted",
            "network": {"access": "restricted", "allowed_domains": ["pypi.org"]},
        },
    )
    assert created.status_code == 200
    session_id = uuid.UUID(created.json()["id"])
    context = await build_turn_context(store, settings, _tenant(token), session_id)
    assert context["env_credentials"] == [
        {
            "credential_id": cred_id,
            "secret_name": "FORGEJO_TOKEN",
            "secret_value": "forgejo-secret",
            "allowed_hosts": ["git.example.com"],
            "git_username": "forgejo-bot",
        }
    ]
    parsed = TurnContext.model_validate(context)
    assert "forgejo-secret" not in repr(parsed)
    assert "forgejo-secret" not in str(redact_context(context))
    typed = env_credentials_from_context(context)
    assert typed[0].secret_value == "forgejo-secret"
    assert env_credential_hosts(typed) == ("git.example.com",)
    rotated = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials/{cred_id}",
        headers=_auth(token),
        json={"auth": {"type": "environment_variable", "secret_value": "rotated"}},
    )
    assert rotated.status_code == 200
    again = await build_turn_context(store, settings, _tenant(token), session_id)
    assert again["env_credentials"][0]["secret_value"] == "rotated"


async def test_context_rechecks_vaults_that_changed(
    settings: Settings, store: Store, client: AsyncClient
) -> None:
    token = "env-recheck"
    agent_id = await _agent(client, token)
    first = await _vault(client, token)
    second = await _vault(client, token)
    await _env_cred(client, token, first)
    hosted = await _session(client, token, agent_id, [first, second])
    assert hosted.status_code == 200
    chat = await _session(client, token, agent_id, [second], {"type": "none"})
    assert chat.status_code == 200
    await _env_cred(client, token, second)
    with pytest.raises(ApiError) as clash:
        await build_turn_context(
            store, settings, _tenant(token), uuid.UUID(hosted.json()["id"])
        )
    assert clash.value.code == "secret_name_collision"
    context = await build_turn_context(
        store, settings, _tenant(token), uuid.UUID(chat.json()["id"])
    )
    assert context["env_credentials"] == []


async def test_agent_export_never_contains_secret_values(client: AsyncClient) -> None:
    token = "env-export"
    vault_id = await _vault(client, token)
    await _env_cred(client, token, vault_id, secret_value="export-must-not-see")
    agent_id = await _agent(client, token, {"vault_ids": [vault_id]})
    exported = await client.get(
        f"/v1/apipi/agents/{agent_id}/export", headers=_auth(token)
    )
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        for name in archive.namelist():
            assert b"export-must-not-see" not in archive.read(name)
    assert b"export-must-not-see" not in exported.content
    template = await client.post(
        "/v1/apipi/templates", headers=_auth(token), json={"agent_id": agent_id}
    )
    assert template.status_code == 200
    assert "export-must-not-see" not in template.text
