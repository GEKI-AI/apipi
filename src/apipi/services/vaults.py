import uuid
from typing import Any, Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select

from apipi.config import Settings
from apipi.gateway.auth import not_found
from apipi.gateway.schemas import StrictModel
from apipi.services.vault_crypto import (
    encrypt_vault_token,
    is_vault_ciphertext,
    vault_aad,
    vault_key_bytes,
)
from apipi.store.engine import Store
from apipi.store.models import Vault, VaultCredential
from apipi.store.repo import (
    create_vault,
    create_vault_credential,
    delete_vault,
    delete_vault_credential,
    get_vault,
    get_vault_credential,
    list_vault_credentials,
    list_vaults,
    update_vault,
    update_vault_credential,
)


async def encrypt_plaintext_vault_tokens(store: Store, settings: Settings) -> int:
    key = vault_key_bytes(settings.vault_master_key)
    rewritten = 0
    async with store.session() as db:
        rows = list(await db.scalars(select(VaultCredential)))
        for row in rows:
            if is_vault_ciphertext(row.token):
                continue
            row.token = encrypt_vault_token(
                row.token, key, aad=vault_aad(row.tenant_id, row.id)
            )
            rewritten += 1
    return rewritten


class VaultWrite(StrictModel):
    name: str | None = None
    metadata: dict[str, Any] | None = None


class StaticBearerAuth(StrictModel):
    type: Literal["static_bearer"]
    mcp_server_url: str
    token: str


class CredentialWrite(StrictModel):
    name: str | None = None
    auth: dict[str, Any]

    @model_validator(mode="after")
    def known_auth(self) -> "CredentialWrite":
        auth_type = self.auth.get("type")
        if auth_type == "mcp_oauth":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "mcp_oauth"},
            )
        if auth_type != "static_bearer":
            raise PydanticCustomError(
                "invalid_request",
                "auth.type must be static_bearer",
                {},
            )
        url = self.auth.get("mcp_server_url")
        token = self.auth.get("token")
        if (
            not isinstance(url, str)
            or not url
            or not isinstance(token, str)
            or not token
        ):
            raise PydanticCustomError(
                "invalid_request",
                "static_bearer needs mcp_server_url and token",
                {},
            )
        return self


class CredentialUpdate(StrictModel):
    name: str | None = None
    auth: dict[str, Any] | None = None

    @model_validator(mode="after")
    def known_auth(self) -> "CredentialUpdate":
        if self.auth is None:
            return self
        auth_type = self.auth.get("type")
        if auth_type == "mcp_oauth":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "mcp_oauth"},
            )
        if auth_type not in {None, "static_bearer"}:
            raise PydanticCustomError(
                "invalid_request",
                "auth.type must be static_bearer",
                {},
            )
        token = self.auth.get("token")
        if token is not None and (not isinstance(token, str) or not token):
            raise PydanticCustomError(
                "invalid_request",
                "token must be a non-empty string",
                {},
            )
        return self


def vault_body(row: Vault) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "metadata": row.metadata_json,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def credential_body(row: VaultCredential) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "vault_id": str(row.vault_id),
        "name": row.name,
        "auth": {
            "type": row.auth_type,
            "mcp_server_url": row.mcp_server_url,
        },
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


class VaultService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def _encrypt_token(
        self, plaintext: str, tenant_id: uuid.UUID, credential_id: uuid.UUID
    ) -> str:
        return encrypt_vault_token(
            plaintext,
            vault_key_bytes(self.settings.vault_master_key),
            aad=vault_aad(tenant_id, credential_id),
        )

    async def create(self, tenant_id: uuid.UUID, body: VaultWrite) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await create_vault(
                db, tenant_id, name=body.name, metadata=body.metadata
            )
            return vault_body(row)

    async def list(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_vaults(db, tenant_id)
            return {"data": [vault_body(row) for row in rows]}

    async def get(self, tenant_id: uuid.UUID, vault_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_vault(db, tenant_id, vault_id)
            if row is None:
                not_found()
            return vault_body(row)

    async def update(
        self, tenant_id: uuid.UUID, vault_id: uuid.UUID, body: VaultWrite
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await update_vault(
                db, tenant_id, vault_id, name=body.name, metadata=body.metadata
            )
            if row is None:
                not_found()
            return vault_body(row)

    async def delete(self, tenant_id: uuid.UUID, vault_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_vault(db, tenant_id, vault_id):
                not_found()
        return {"id": str(vault_id), "deleted": True}

    async def create_credential(
        self, tenant_id: uuid.UUID, vault_id: uuid.UUID, body: CredentialWrite
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            vault = await get_vault(db, tenant_id, vault_id)
            if vault is None:
                not_found()
            credential_id = uuid.uuid4()
            row = await create_vault_credential(
                db,
                tenant_id,
                vault_id,
                name=body.name,
                auth_type="static_bearer",
                mcp_server_url=str(body.auth["mcp_server_url"]),
                token=self._encrypt_token(
                    str(body.auth["token"]), tenant_id, credential_id
                ),
                credential_id=credential_id,
            )
            return credential_body(row)

    async def list_credentials(
        self, tenant_id: uuid.UUID, vault_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            vault = await get_vault(db, tenant_id, vault_id)
            if vault is None:
                not_found()
            rows = await list_vault_credentials(db, tenant_id, vault_id)
            return {"data": [credential_body(row) for row in rows]}

    async def get_credential(
        self,
        tenant_id: uuid.UUID,
        vault_id: uuid.UUID,
        credential_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_vault_credential(db, tenant_id, vault_id, credential_id)
            if row is None:
                not_found()
            return credential_body(row)

    async def update_credential(
        self,
        tenant_id: uuid.UUID,
        vault_id: uuid.UUID,
        credential_id: uuid.UUID,
        body: CredentialUpdate,
    ) -> dict[str, Any]:
        token = None
        if body.auth is not None and isinstance(body.auth.get("token"), str):
            token = self._encrypt_token(body.auth["token"], tenant_id, credential_id)
        async with self.store.session() as db:
            row = await update_vault_credential(
                db,
                tenant_id,
                vault_id,
                credential_id,
                name=body.name,
                token=token,
            )
            if row is None:
                not_found()
            return credential_body(row)

    async def delete_credential(
        self,
        tenant_id: uuid.UUID,
        vault_id: uuid.UUID,
        credential_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_vault_credential(
                db, tenant_id, vault_id, credential_id
            ):
                not_found()
        return {"id": str(credential_id), "deleted": True}
