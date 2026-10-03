from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile

from apipi.common.objects import NS_SKILLS
from apipi.gateway.auth import check_authorize, require_tenant
from apipi.store.blobs import skill_object_id
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
    await check_authorize(
        request, action="skill.write", resource_type="skill", resource_id=None
    )
    return await _skills(request).create(tenant.id, data=data, filename=filename)


@router.get("/v1/skills")
async def list_skills(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    filt = await check_authorize(
        request, action="skill.list", resource_type="skill", resource_id=None
    )
    payload = await _skills(request).list_objects(tenant.id)
    if filt is not None and filt.ids is not None:
        items = payload.get("skills", payload.get("data", []))
        key = (
            "skills" if "skills" in payload else ("data" if "data" in payload else None)
        )
        if key is not None:
            payload = dict(payload)
            payload[key] = [s for s in items if str(s.get("id")) in filt.ids]
    return payload


@router.get("/v1/skills/{skill_id}")
async def read_skill(
    skill_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_skill as _gs

        if await _gs(_db, tenant.id, skill_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="skill.read", resource_type="skill", resource_id=str(skill_id)
    )
    return await _skills(request).get(tenant.id, skill_id)


@router.post("/v1/apipi/skills/{skill_id}/download")
async def download_skill(
    skill_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_skill as _gs

        if await _gs(_db, tenant.id, skill_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="skill.read", resource_type="skill", resource_id=str(skill_id)
    )
    body = await _skills(request).get(tenant.id, skill_id)
    name = body.get("name")
    filename = f"{name}.zip" if isinstance(name, str) and name else "skill.zip"
    return request.app.state.gateway.uploads.download(
        NS_SKILLS,
        skill_object_id(tenant.id, skill_id),
        filename=filename,
        content_type="application/zip",
    )


@router.delete("/v1/skills/{skill_id}")
async def remove_skill(
    skill_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_skill as _gs

        if await _gs(_db, tenant.id, skill_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="skill.write", resource_type="skill", resource_id=str(skill_id)
    )
    return await _skills(request).delete(tenant.id, skill_id)
