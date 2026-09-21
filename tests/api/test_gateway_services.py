import logging
import uuid

import httpx
import pytest

from apipi.config import VAULT_MASTER_KEY_UNSET, Settings
from apipi.gateway import Gateway
from apipi.gateway.errors import ApiError
from apipi.services.agents import AgentWrite
from apipi.services.runtime import FakeHarness
from apipi.services.vaults import CredentialWrite, VaultWrite
from apipi.store.engine import Store


async def test_ensure_tenant(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    tenant_id = uuid.uuid4()
    first = await gateway.ensure_tenant(tenant_id)
    again = await gateway.ensure_tenant(tenant_id)
    assert first.id == tenant_id
    assert again.id == tenant_id


async def test_in_process_agents_crud(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    tenant_id = uuid.uuid4()
    await gateway.ensure_tenant(tenant_id)
    created = await gateway.agents.create(
        tenant_id, AgentWrite(name="one", model="test")
    )
    assert created["name"] == "one"
    agent_id = uuid.UUID(created["id"])
    listed = await gateway.agents.list(tenant_id)
    assert [row["id"] for row in listed["data"]] == [created["id"]]
    got = await gateway.agents.get(tenant_id, agent_id)
    assert got["model"] == "test"
    updated = await gateway.agents.update(tenant_id, agent_id, AgentWrite(name="two"))
    assert updated["name"] == "two"
    deleted = await gateway.agents.delete(tenant_id, agent_id)
    assert deleted == {"id": str(agent_id), "deleted": True}
    with pytest.raises(ApiError) as exc:
        await gateway.agents.get(tenant_id, agent_id)
    assert exc.value.status_code == 404


async def test_gateway_startup_warns_without_vault_key(
    settings: Settings, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    await gateway.startup()
    try:
        assert VAULT_MASTER_KEY_UNSET in caplog.text
    finally:
        await gateway.shutdown()


async def test_gateway_startup_quiet_with_vault_key(
    settings: Settings, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    import base64

    caplog.set_level(logging.WARNING, logger="apipi")
    key = base64.b64encode(bytes(range(32))).decode()
    gateway = Gateway.create(
        settings.model_copy(update={"vault_master_key": key}),
        store=store,
        harness=FakeHarness(),
    )
    await gateway.startup()
    try:
        assert VAULT_MASTER_KEY_UNSET not in caplog.text
    finally:
        await gateway.shutdown()


async def test_in_process_vaults_omit_token(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    tenant_id = uuid.uuid4()
    await gateway.ensure_tenant(tenant_id)
    vault = await gateway.vaults.create(tenant_id, VaultWrite(name="GitHub"))
    vault_id = uuid.UUID(vault["id"])
    cred = await gateway.vaults.create_credential(
        tenant_id,
        vault_id,
        CredentialWrite(
            name="pat",
            auth={
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "secret-token",
            },
        ),
    )
    assert "token" not in cred["auth"]
    assert "secret-token" not in str(cred)
    got = await gateway.vaults.get_credential(
        tenant_id, vault_id, uuid.UUID(cred["id"])
    )
    assert "token" not in got["auth"]
    listed = await gateway.vaults.list_credentials(tenant_id, vault_id)
    assert "token" not in listed["data"][0]["auth"]


async def test_in_process_usage_needs_one_filter(
    settings: Settings, store: Store
) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    tenant_id = uuid.uuid4()
    await gateway.ensure_tenant(tenant_id)
    with pytest.raises(ApiError) as exc:
        await gateway.usage.get(tenant_id)
    assert exc.value.code == "invalid_request"


async def test_in_process_models_list(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "object": "list",
        "data": [{"id": "gpt-4.1", "object": "model", "owned_by": "host"}],
    }

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/models")
        return httpx.Response(200, json=payload)

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", fake_get)
    gateway = Gateway.create(
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        store=store,
        harness=FakeHarness(),
    )
    assert gateway.models.list("t") == payload
