import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.auth import get_db, not_found, require_tenant
from apipi.schemas import StrictModel
from apipi.store.models import Tenant, Vault, VaultCredential
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

router = APIRouter()


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


@router.post("/v1/agents/vaults")
async def create_saved_vault(
    body: VaultWrite,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await create_vault(db, tenant.id, name=body.name, metadata=body.metadata)
    return vault_body(row)


@router.get("/v1/agents/vaults")
async def list_saved_vaults(
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    rows = await list_vaults(db, tenant.id)
    return {"data": [vault_body(row) for row in rows]}


@router.get("/v1/agents/vaults/{vault_id}")
async def read_saved_vault(
    vault_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_vault(db, tenant.id, vault_id)
    if row is None:
        not_found()
    return vault_body(row)


@router.post("/v1/agents/vaults/{vault_id}")
async def update_saved_vault(
    vault_id: uuid.UUID,
    body: VaultWrite,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await update_vault(
        db, tenant.id, vault_id, name=body.name, metadata=body.metadata
    )
    if row is None:
        not_found()
    return vault_body(row)


@router.delete("/v1/agents/vaults/{vault_id}")
async def delete_saved_vault(
    vault_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    if not await delete_vault(db, tenant.id, vault_id):
        not_found()
    return {"id": str(vault_id), "deleted": True}


@router.post("/v1/agents/vaults/{vault_id}/credentials")
async def create_saved_credential(
    vault_id: uuid.UUID,
    body: CredentialWrite,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    vault = await get_vault(db, tenant.id, vault_id)
    if vault is None:
        not_found()
    row = await create_vault_credential(
        db,
        tenant.id,
        vault_id,
        name=body.name,
        auth_type="static_bearer",
        mcp_server_url=str(body.auth["mcp_server_url"]),
        token=str(body.auth["token"]),
    )
    return credential_body(row)


@router.get("/v1/agents/vaults/{vault_id}/credentials")
async def list_saved_credentials(
    vault_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    vault = await get_vault(db, tenant.id, vault_id)
    if vault is None:
        not_found()
    rows = await list_vault_credentials(db, tenant.id, vault_id)
    return {"data": [credential_body(row) for row in rows]}


@router.get("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def read_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_vault_credential(db, tenant.id, vault_id, credential_id)
    if row is None:
        not_found()
    return credential_body(row)


@router.post("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def update_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    body: CredentialUpdate,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    token = None
    if body.auth is not None and isinstance(body.auth.get("token"), str):
        token = body.auth["token"]
    row = await update_vault_credential(
        db,
        tenant.id,
        vault_id,
        credential_id,
        name=body.name,
        token=token,
    )
    if row is None:
        not_found()
    return credential_body(row)


@router.delete("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def delete_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    if not await delete_vault_credential(db, tenant.id, vault_id, credential_id):
        not_found()
    return {"id": str(credential_id), "deleted": True}
