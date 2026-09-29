import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from apipi.api.deps import model_key
from apipi.gateway.auth import require_tenant
from apipi.gateway.schemas import StrictModel
from apipi.store.models import Tenant

router = APIRouter()


class VersionCreate(StrictModel):
    name: str | None = None
    comment: str | None = None


def _versions(request: Request) -> Any:
    return request.app.state.gateway.versions


def _actor(request: Request) -> str | None:
    key_id = getattr(request.state, "key_id", None)
    return key_id if isinstance(key_id, str) and key_id else None


@router.post("/v1/apipi/agents/{agent_id}/versions")
async def create_version(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    body: VersionCreate | None = None,
) -> dict[str, Any]:
    payload = body if body is not None else VersionCreate()
    return await _versions(request).create_explicit(
        tenant.id,
        agent_id,
        name=payload.name,
        comment=payload.comment,
        created_by=_actor(request),
    )


@router.get("/v1/apipi/agents/{agent_id}/versions")
async def list_versions(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    limit: Annotated[int, Query()] = 20,
    after: Annotated[str | None, Query()] = None,
    include: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return await _versions(request).list_versions(
        tenant.id,
        agent_id,
        limit=limit,
        after=after,
        include_definition=include == "definition",
    )


@router.get("/v1/apipi/agents/{agent_id}/versions/{version}")
async def read_version(
    agent_id: uuid.UUID,
    version: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _versions(request).get_version(tenant.id, agent_id, version)


@router.post("/v1/apipi/agents/{agent_id}/versions/{version}/restore")
async def restore_version(
    agent_id: uuid.UUID,
    version: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _versions(request).restore(
        tenant.id,
        agent_id,
        version,
        created_by=_actor(request),
        api_key=model_key(request),
    )


@router.delete("/v1/apipi/agents/{agent_id}/versions/{version}")
async def delete_version(
    agent_id: uuid.UUID,
    version: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _versions(request).delete_version(tenant.id, agent_id, version)
