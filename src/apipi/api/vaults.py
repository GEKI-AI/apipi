import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.auth import require_tenant
from apipi.store.models import Tenant
from apipi.vaults import CredentialUpdate, CredentialWrite, VaultWrite

router = APIRouter()


def _vaults(request: Request) -> Any:
    return request.app.state.gateway.vaults


@router.post("/v1/agents/vaults")
async def create_saved_vault(
    body: VaultWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).create(tenant.id, body)


@router.get("/v1/agents/vaults")
async def list_saved_vaults(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).list(tenant.id)


@router.get("/v1/agents/vaults/{vault_id}")
async def read_saved_vault(
    vault_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).get(tenant.id, vault_id)


@router.post("/v1/agents/vaults/{vault_id}")
async def update_saved_vault(
    vault_id: uuid.UUID,
    body: VaultWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).update(tenant.id, vault_id, body)


@router.delete("/v1/agents/vaults/{vault_id}")
async def delete_saved_vault(
    vault_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).delete(tenant.id, vault_id)


@router.post("/v1/agents/vaults/{vault_id}/credentials")
async def create_saved_credential(
    vault_id: uuid.UUID,
    body: CredentialWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).create_credential(tenant.id, vault_id, body)


@router.get("/v1/agents/vaults/{vault_id}/credentials")
async def list_saved_credentials(
    vault_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).list_credentials(tenant.id, vault_id)


@router.get("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def read_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).get_credential(tenant.id, vault_id, credential_id)


@router.post("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def update_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    body: CredentialUpdate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).update_credential(
        tenant.id, vault_id, credential_id, body
    )


@router.delete("/v1/agents/vaults/{vault_id}/credentials/{credential_id}")
async def delete_saved_credential(
    vault_id: uuid.UUID,
    credential_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _vaults(request).delete_credential(tenant.id, vault_id, credential_id)
