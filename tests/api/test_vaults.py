import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthFilter, AuthIdentity, AuthReject
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


async def test_vault_list_applies_authorize_filter(
    settings: Settings, store: Store
) -> None:
    allowed: set[str] = set()

    async def authorize(
        identity: AuthIdentity, action: str, resource_type: str, resource_id: str | None
    ) -> AuthFilter | AuthReject | None:
        if action == "vault.list" and allowed:
            return AuthFilter(ids=frozenset(allowed))
        return None

    app = create_app(api_settings_for(settings), store=store, authorize=authorize)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = _auth("vault-filter")
        first = await client.post(
            "/v1/agents/vaults", headers=headers, json={"name": "A"}
        )
        second = await client.post(
            "/v1/agents/vaults", headers=headers, json={"name": "B"}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        vault_a = first.json()["id"]
        allowed.add(vault_a)
        listed = await client.get("/v1/agents/vaults", headers=headers)
        assert listed.status_code == 200
        body = listed.json()
        assert [v["id"] for v in body["data"]] == [vault_a]
        assert "vaults" not in body


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


async def _vault(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "git"}
    )
    assert created.status_code == 200
    return str(created.json()["id"])


def _env_auth(**overrides: object) -> dict[str, object]:
    auth: dict[str, object] = {
        "type": "environment_variable",
        "secret_name": "GITHUB_TOKEN",
        "secret_value": "ghp_live_secret",
        "networking": {
            "type": "limited",
            "allowed_hosts": ["github.com", "API.GitHub.com"],
        },
    }
    auth.update(overrides)
    return auth


async def test_env_credential_crud_never_returns_secret(client: AsyncClient) -> None:
    token = "env-crud"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    created = await client.post(
        base,
        headers=_auth(token),
        json={
            "name": "github",
            "auth": _env_auth(),
            "metadata": {"apipi.git_username": "octocat"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["auth"] == {
        "type": "environment_variable",
        "secret_name": "GITHUB_TOKEN",
        "networking": {
            "type": "limited",
            "allowed_hosts": ["github.com", "api.github.com"],
        },
    }
    assert body["metadata"] == {"apipi.git_username": "octocat"}
    cred_id = body["id"]
    listed = await client.get(base, headers=_auth(token))
    got = await client.get(f"{base}/{cred_id}", headers=_auth(token))
    for response in (created, listed, got):
        assert "ghp_live_secret" not in response.text
        assert "secret_value" not in response.text
    assert listed.json()["data"][0]["auth"]["secret_name"] == "GITHUB_TOKEN"
    assert got.json()["auth"]["networking"]["type"] == "limited"
    updated = await client.post(
        f"{base}/{cred_id}",
        headers=_auth(token),
        json={
            "name": "github-rotated",
            "auth": {
                "type": "environment_variable",
                "secret_value": "ghp_rotated",
                "secret_name": "GITHUB_TOKEN",
                "networking": {
                    "type": "limited",
                    "allowed_hosts": ["api.github.com", "github.com"],
                },
            },
        },
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == cred_id
    assert updated.json()["name"] == "github-rotated"
    assert updated.json()["auth"]["type"] == "environment_variable"
    assert "ghp_rotated" not in updated.text
    deleted = await client.delete(f"{base}/{cred_id}", headers=_auth(token))
    assert deleted.status_code == 200
    gone = await client.get(f"{base}/{cred_id}", headers=_auth(token))
    assert gone.status_code == 404


async def test_env_credential_secret_is_encrypted(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from apipi.services.vault_crypto import (
        decrypt_vault_token,
        is_vault_ciphertext,
        vault_aad,
        vault_key_bytes,
    )
    from apipi.store.models import VaultCredential

    token = "env-store"
    vault_id = await _vault(client, token)
    cred = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={"auth": _env_auth(secret_value="keep-env-secret")},
    )
    assert cred.status_code == 200
    async with store.session() as db:
        found = await db.scalar(
            select(VaultCredential).where(VaultCredential.id == UUID(cred.json()["id"]))
        )
    assert found is not None
    assert found.mcp_server_url is None
    assert found.secret_name == "GITHUB_TOKEN"
    assert found.allowed_hosts == ["github.com", "api.github.com"]
    assert is_vault_ciphertext(found.token)
    assert "keep-env-secret" not in found.token
    assert (
        decrypt_vault_token(
            found.token,
            vault_key_bytes(settings.vault_master_key),
            aad=vault_aad(found.tenant_id, found.id),
        )
        == "keep-env-secret"
    )


def _hosts(*hosts: str) -> dict[str, object]:
    return {"networking": {"type": "limited", "allowed_hosts": list(hosts)}}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"secret_name": "1TOKEN"}, "environment variable name"),
        ({"secret_name": "MY-TOKEN"}, "environment variable name"),
        ({"secret_name": ""}, "secret_name"),
        ({"secret_name": "OPENAI_API_KEY"}, "reserved"),
        ({"secret_name": "OPENAI_ORG"}, "reserved"),
        ({"secret_name": "APIPI_X"}, "reserved"),
        ({"secret_name": "PI_X"}, "reserved"),
        ({"secret_name": "PATH"}, "reserved"),
        ({"secret_name": "HOME"}, "reserved"),
        ({"secret_name": "NODE_OPTIONS"}, "reserved"),
        ({"secret_name": "LD_PRELOAD"}, "reserved"),
        ({"secret_name": "SSL_CERT_FILE"}, "reserved"),
        ({"secret_name": "REQUESTS_CA_BUNDLE"}, "reserved"),
        ({"secret_name": "GIT_SSL_CAINFO"}, "reserved"),
        ({"secret_name": "NODE_EXTRA_CA_CERTS"}, "reserved"),
        ({"secret_value": ""}, "secret_value"),
        ({"secret_value": 5}, "secret_value"),
        ({"secret_value": "line\nbreak"}, "control characters"),
        ({"secret_value": "with space"}, "printable ASCII"),
        ({"secret_value": "nonascii-\u00e9-value"}, "printable ASCII"),
        ({"secret_value": "short7!"}, "at least 8"),
        ({"secret_name": "CURL_CA_BUNDLE"}, "reserved"),
        ({"secret_name": "UV_CACHE_DIR"}, "reserved"),
        ({"secret_name": "npm_config_cache"}, "reserved"),
        ({"secret_name": "GIT_CONFIG_COUNT"}, "reserved"),
        ({"secret_name": "GIT_CONFIG_KEY_3"}, "reserved"),
        ({"secret_name": "GIT_CONFIG_VALUE_12"}, "reserved"),
        ({"secret_name": "WS"}, "reserved"),
        ({"secret_name": "GIT_SSL_NO_VERIFY"}, "reserved"),
        ({"secret_name": "GIT_CONFIG_PARAMETERS"}, "reserved"),
        ({"secret_name": "AGENT_BROWSER_ARGS"}, "reserved"),
        ({"networking": None}, "networking"),
        ({"networking": {"type": "open", "allowed_hosts": ["a.com"]}}, "limited"),
        (_hosts(), "at least one"),
        (_hosts("https://a.com"), "exact hostname"),
        (_hosts("a.com:443"), "exact hostname"),
        (_hosts("a.com/x"), "exact hostname"),
        (_hosts("*.a.com"), "exact hostname"),
        (_hosts("10.0.0.1"), "IP address"),
        (_hosts("[::1]"), "IP address"),
        (_hosts("127.1"), "exact hostname"),
        (_hosts(*[f"h{index}.example.com" for index in range(101)]), "at most 100"),
    ],
)
async def test_env_credential_validation(
    client: AsyncClient, overrides: dict[str, object], fragment: str
) -> None:
    token = "env-validate"
    vault_id = await _vault(client, token)
    response = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={"auth": _env_auth(**overrides)},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert fragment in error["message"]


async def test_env_credential_unknown_auth_field(client: AsyncClient) -> None:
    token = "env-unknown"
    vault_id = await _vault(client, token)
    response = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={"auth": _env_auth(token="x")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_field"


async def test_env_credential_secret_name_unique_per_vault(client: AsyncClient) -> None:
    token = "env-unique"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    first = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    assert first.status_code == 200
    second = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "secret_name_collision"
    other_vault = await _vault(client, token)
    third = await client.post(
        f"/v1/agents/vaults/{other_vault}/credentials",
        headers=_auth(token),
        json={"auth": _env_auth()},
    )
    assert third.status_code == 200


@pytest.mark.parametrize(
    ("auth", "fragment"),
    [
        (
            {"type": "environment_variable", "secret_name": "GH_TOKEN"},
            "secret_name cannot change",
        ),
        (
            {"type": "environment_variable", **_hosts("gitlab.com")},
            "networking cannot change",
        ),
        ({"type": "static_bearer", "token": "x"}, "auth.type cannot change"),
        ({"type": "environment_variable", "secret_value": ""}, "secret_value"),
    ],
)
async def test_env_credential_update_rules(
    client: AsyncClient, auth: dict[str, object], fragment: str
) -> None:
    token = "env-update"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    created = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    response = await client.post(
        f"{base}/{created.json()['id']}", headers=_auth(token), json={"auth": auth}
    )
    assert response.status_code == 400
    assert fragment in response.json()["error"]["message"]


async def test_env_credential_git_username_metadata(client: AsyncClient) -> None:
    token = "env-git-user"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    bad = await client.post(
        base,
        headers=_auth(token),
        json={"auth": _env_auth(), "metadata": {"apipi.git_username": "a:b"}},
    )
    assert bad.status_code == 400
    static = await client.post(
        base,
        headers=_auth(token),
        json={
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "t",
            },
            "metadata": {"apipi.git_username": "bot"},
        },
    )
    assert static.status_code == 400
    created = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    assert created.json()["metadata"] == {}
    updated = await client.post(
        f"{base}/{created.json()['id']}",
        headers=_auth(token),
        json={"metadata": {"apipi.git_username": "forgejo-bot"}},
    )
    assert updated.status_code == 200
    assert updated.json()["metadata"] == {"apipi.git_username": "forgejo-bot"}


async def test_static_bearer_errors_unchanged(client: AsyncClient) -> None:
    token = "static-errors"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    missing = await client.post(
        base,
        headers=_auth(token),
        json={"auth": {"type": "static_bearer", "token": "x"}},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "validation_error"
    unknown = await client.post(
        base, headers=_auth(token), json={"auth": {"type": "basic"}}
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "validation_error"


async def test_encrypt_plaintext_env_secret(settings: Settings, store: Store) -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from apipi.services.vault_crypto import is_vault_ciphertext
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
            auth_type="environment_variable",
            token="legacy-env-plain",
            secret_name="FORGEJO_TOKEN",
            allowed_hosts=["git.example.com"],
        )
        cred_id = row.id
    assert await encrypt_plaintext_vault_tokens(store, settings) == 1
    async with store.session() as db:
        found = await db.scalar(
            select(VaultCredential).where(VaultCredential.id == cred_id)
        )
    assert found is not None
    assert is_vault_ciphertext(found.token)


async def test_env_credential_unique_constraint_maps_to_collision(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "env-race"
    vault_id = await _vault(client, token)
    base = f"/v1/agents/vaults/{vault_id}/credentials"
    first = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    assert first.status_code == 200

    async def no_rows(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr("apipi.services.vaults.list_vault_credentials", no_rows)
    second = await client.post(base, headers=_auth(token), json={"auth": _env_auth()})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "secret_name_collision"


@pytest.mark.parametrize(
    "metadata",
    [
        {f"k{index}": "v" for index in range(17)},
        {"k" * 65: "v"},
        {"k": "v" * 513},
        {"k": 5},
    ],
)
async def test_credential_metadata_limits(
    client: AsyncClient, metadata: dict[str, object]
) -> None:
    token = "env-meta-limits"
    vault_id = await _vault(client, token)
    response = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={"auth": _env_auth(), "metadata": metadata},
    )
    assert response.status_code == 400
    assert "metadata" in response.json()["error"]["message"]
