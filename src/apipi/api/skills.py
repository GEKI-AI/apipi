from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile

from apipi.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


def _skills(request: Request) -> Any:
    return request.app.state.gateway.skill_store


@router.post("/v1/skills")
async def upload_skill(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    files: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    data = await files.read()
    filename = files.filename or "skill.zip"
    return await _skills(request).create(tenant.id, data=data, filename=filename)


@router.get("/v1/skills")
async def list_skills(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _skills(request).list_objects(tenant.id)


@router.get("/v1/skills/{skill_id}")
async def read_skill(
    skill_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _skills(request).get(tenant.id, skill_id)


@router.delete("/v1/skills/{skill_id}")
async def remove_skill(
    skill_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _skills(request).delete(tenant.id, skill_id)
