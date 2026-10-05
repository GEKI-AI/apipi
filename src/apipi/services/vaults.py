import uuid
from typing import Any, NoReturn

from pydantic import model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apipi.common.errors import ApiError
from apipi.config import Settings
from apipi.gateway.auth import not_found
from apipi.gateway.schemas import StrictModel
from apipi.services.env_credentials import (
    ENVIRONMENT_VARIABLE,
    STATIC_BEARER,
    credential_metadata,
    networking_body,
    parse_environment_auth,
    parse_environment_update,
)
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


SECRET_NAME_CONSTRAINT = "vault_credentials_secret_name_key"


def _sqlite_unique(exc: IntegrityError) -> bool:
    text = str(exc.orig)
    return (
        "UNIQUE constraint failed" in text and "vault_credentials.secret_name" in text
    )


def _secret_name_taken(secret_name: str) -> NoReturn:
    raise ApiError(
        "invalid_request",
        f"secret_name {secret_name} is already used by a credential in this vault",
        code="secret_name_collision",
    )


class VaultWrite(StrictModel):
    name: str | None = None
    metadata: dict[str, Any] | None = None


def _reject_oauth(auth_type: object) -> None:
    if auth_type == "mcp_oauth":
        raise PydanticCustomError(
            "not_implemented",
            "{field} is not implemented",
            {"field": "mcp_oauth"},
        )


class CredentialWrite(StrictModel):
    name: str | None = None
    metadata: dict[str, Any] | None = None
    auth: dict[str, Any]

    @model_validator(mode="after")
    def known_auth(self) -> "CredentialWrite":
        auth_type = self.auth.get("type")
        _reject_oauth(auth_type)
        if auth_type == ENVIRONMENT_VARIABLE:
            return self
        if auth_type != STATIC_BEARER:
            raise PydanticCustomError(
                "invalid_request",
                "auth.type must be static_bearer or environment_variable",
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
    metadata: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None

    @model_validator(mode="after")
    def known_auth(self) -> "CredentialUpdate":
        if self.auth is None:
            return self
        auth_type = self.auth.get("type")
        _reject_oauth(auth_type)
        if auth_type == ENVIRONMENT_VARIABLE:
            return self
        if auth_type not in {None, STATIC_BEARER}:
            raise PydanticCustomError(
                "invalid_request",
                "auth.type must be static_bearer or environment_variable",
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


def credential_auth_body(row: VaultCredential) -> dict[str, Any]:
    if row.auth_type == ENVIRONMENT_VARIABLE:
        return {
            "type": row.auth_type,
            "secret_name": row.secret_name,
            "networking": networking_body(row.allowed_hosts),
        }
    return {"type": row.auth_type, "mcp_server_url": row.mcp_server_url}


def credential_body(row: VaultCredential) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "vault_id": str(row.vault_id),
        "name": row.name,
        "auth": credential_auth_body(row),
        "metadata": row.metadata_json if isinstance(row.metadata_json, dict) else {},
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
        if body.auth.get("type") == ENVIRONMENT_VARIABLE:
            return await self._create_env_credential(tenant_id, vault_id, body)
        metadata = credential_metadata(body.metadata, STATIC_BEARER)
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
                auth_type=STATIC_BEARER,
                mcp_server_url=str(body.auth["mcp_server_url"]),
                token=self._encrypt_token(
                    str(body.auth["token"]), tenant_id, credential_id
                ),
                credential_id=credential_id,
                metadata=metadata,
            )
            return credential_body(row)

    async def _create_env_credential(
        self, tenant_id: uuid.UUID, vault_id: uuid.UUID, body: CredentialWrite
    ) -> dict[str, Any]:
        auth = parse_environment_auth(body.auth)
        metadata = credential_metadata(body.metadata, ENVIRONMENT_VARIABLE)
        try:
            async with self.store.session() as db:
                vault = await get_vault(db, tenant_id, vault_id)
                if vault is None:
                    not_found()
                for existing in await list_vault_credentials(db, tenant_id, vault_id):
                    if existing.secret_name == auth.secret_name:
                        _secret_name_taken(auth.secret_name)
                credential_id = uuid.uuid4()
                row = await create_vault_credential(
                    db,
                    tenant_id,
                    vault_id,
                    name=body.name,
                    auth_type=ENVIRONMENT_VARIABLE,
                    token=self._encrypt_token(
                        auth.secret_value, tenant_id, credential_id
                    ),
                    credential_id=credential_id,
                    secret_name=auth.secret_name,
                    allowed_hosts=list(auth.allowed_hosts),
                    metadata=metadata,
                )
                return credential_body(row)
        except IntegrityError as exc:
            if SECRET_NAME_CONSTRAINT not in str(exc.orig) and not _sqlite_unique(exc):
                raise
            _secret_name_taken(auth.secret_name)

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
        async with self.store.session() as db:
            current = await get_vault_credential(db, tenant_id, vault_id, credential_id)
            if current is None:
                not_found()
            token = None
            if body.auth is not None:
                auth_type = body.auth.get("type")
                if auth_type is not None and auth_type != current.auth_type:
                    raise ApiError(
                        "invalid_request",
                        "auth.type cannot change; create a new credential",
                        code="invalid_request",
                    )
                if current.auth_type == ENVIRONMENT_VARIABLE:
                    secret = parse_environment_update(body.auth, current)
                    if secret is not None:
                        token = self._encrypt_token(secret, tenant_id, credential_id)
                elif isinstance(body.auth.get("token"), str):
                    token = self._encrypt_token(
                        body.auth["token"], tenant_id, credential_id
                    )
            row = await update_vault_credential(
                db,
                tenant_id,
                vault_id,
                credential_id,
                name=body.name,
                token=token,
                metadata=credential_metadata(body.metadata, current.auth_type),
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
