from httpx import AsyncClient

from apipi.config import Settings
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_vault_crud_omits_token(client: AsyncClient) -> None:
    token = "vault-crud"
    created = await client.post(
        "/v1/agents/vaults",
        headers=_auth(token),
        json={"name": "GitHub", "metadata": {"k": "v"}},
    )
    assert created.status_code == 200
    vault_id = created.json()["id"]
    assert created.json()["name"] == "GitHub"
    listed = await client.get("/v1/agents/vaults", headers=_auth(token))
    assert listed.json()["data"][0]["id"] == vault_id
    cred = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={
            "name": "pat",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "secret-token",
            },
        },
    )
    assert cred.status_code == 200
    body = cred.json()
    assert body["auth"]["type"] == "static_bearer"
    assert body["auth"]["mcp_server_url"] == "https://mcp.example.com/mcp"
    assert "token" not in body["auth"]
    assert "secret-token" not in str(body)
    got = await client.get(
        f"/v1/agents/vaults/{vault_id}/credentials/{body['id']}",
        headers=_auth(token),
    )
    assert "token" not in got.json()["auth"]
    other = await client.get(
        f"/v1/agents/vaults/{vault_id}",
        headers=_auth("other-tenant"),
    )
    assert other.status_code == 404


async def test_vault_oauth_is_not_implemented(client: AsyncClient) -> None:
    token = "vault-oauth"
    created = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "x"}
    )
    vault_id = created.json()["id"]
    cred = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={
            "name": "oauth",
            "auth": {
                "type": "mcp_oauth",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "access_token": "x",
            },
        },
    )
    assert cred.status_code == 400
    assert cred.json()["error"]["code"] == "mcp_oauth"


async def test_session_vault_ids_and_unknown_vault(client: AsyncClient) -> None:
    token = "vault-session"
    vault = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "v"}
    )
    vault_id = vault.json()["id"]
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "vault_ids": [vault_id],
        },
    )
    assert created.status_code == 200
    assert created.json()["vault_ids"] == [vault_id]
    missing = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "vault_ids": ["00000000-0000-0000-0000-000000000000"],
        },
    )
    assert missing.status_code == 404


async def test_vault_token_stays_encrypted(client: AsyncClient, store: Store) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from apipi.services.vault_crypto import (
        decrypt_vault_token,
        is_vault_ciphertext,
        vault_aad,
        vault_key_bytes,
    )
    from apipi.store.models import VaultCredential

    token = "vault-store"
    vault = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "v"}
    )
    vault_id = vault.json()["id"]
    cred = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "keep-me",
            }
        },
    )
    async with store.session() as db:
        found = await db.scalar(
            select(VaultCredential).where(VaultCredential.id == UUID(cred.json()["id"]))
        )
        assert found is not None
        assert is_vault_ciphertext(found.token)
        assert found.token != "keep-me"
        assert "keep-me" not in found.token
        assert (
            decrypt_vault_token(
                found.token,
                vault_key_bytes(None),
                aad=vault_aad(found.tenant_id, found.id),
            )
            == "keep-me"
        )


async def test_encrypt_plaintext_vault_tokens(settings: Settings, store: Store) -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from apipi.services.vault_crypto import (
        decrypt_vault_token,
        is_vault_ciphertext,
        vault_aad,
        vault_key_bytes,
    )
    from apipi.services.vaults import encrypt_plaintext_vault_tokens
    from apipi.store.models import VaultCredential
    from apipi.store.repo import create_vault, create_vault_credential, ensure_tenant

    tenant_id = uuid4()
    async with store.session() as db:
        await ensure_tenant(db, tenant_id)
        vault = await create_vault(db, tenant_id, name="v")
        row = await create_vault_credential(
            db,
            tenant_id,
            vault.id,
            auth_type="static_bearer",
            mcp_server_url="https://mcp.example.com/mcp",
            token="legacy-plain",
        )
        cred_id = row.id
    assert await encrypt_plaintext_vault_tokens(store, settings) == 1
    assert await encrypt_plaintext_vault_tokens(store, settings) == 0
    async with store.session() as db:
        found = await db.scalar(
            select(VaultCredential).where(VaultCredential.id == cred_id)
        )
        assert found is not None
        assert is_vault_ciphertext(found.token)
        assert "legacy-plain" not in found.token
        assert (
            decrypt_vault_token(
                found.token,
                vault_key_bytes(settings.vault_master_key),
                aad=vault_aad(tenant_id, cred_id),
            )
            == "legacy-plain"
        )
