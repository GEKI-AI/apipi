import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from apipi.api.deps import model_key
from apipi.gateway.auth import check_authorize, require_tenant
from apipi.services.templates import TemplateAgentCreate, TemplateCreate
from apipi.store.disposition import content_disposition
from apipi.store.models import Tenant

router = APIRouter()


def _templates(request: Request) -> Any:
    return request.app.state.gateway.templates


def _user_id(request: Request) -> str | None:
    value = getattr(request.state, "user_id", None)
    return value if isinstance(value, str) and value else None


def _org_id(request: Request) -> str | None:
    value = getattr(request.state, "org_id", None)
    return value if isinstance(value, str) and value else None


@router.post("/v1/apipi/templates")
async def create_template(
    body: TemplateCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await check_authorize(
        request, action="template.write", resource_type="template", resource_id=None
    )
    return await _templates(request).create_from_agent(
        tenant.id, body, created_by=_user_id(request)
    )


@router.post("/v1/apipi/templates/import")
async def import_template(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    bundle: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    await check_authorize(
        request, action="template.write", resource_type="template", resource_id=None
    )
    data = await bundle.read()
    return await _templates(request).import_bundle(
        tenant.id,
        data,
        name=name,
        description=description,
        created_by=_user_id(request),
    )


@router.get("/v1/apipi/templates")
async def list_templates(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    filt = await check_authorize(
        request, action="template.list", resource_type="template", resource_id=None
    )
    payload = await _templates(request).list_objects(tenant.id)
    if filt is not None and filt.ids is not None:
        items = payload.get("templates", payload.get("data", []))
        key = (
            "templates"
            if "templates" in payload
            else ("data" if "data" in payload else None)
        )
        if key is not None:
            payload = dict(payload)
            payload[key] = [x for x in items if str(x.get("id")) in filt.ids]
    return payload


@router.get("/v1/apipi/templates/{template_id}")
async def read_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_template as _gt

        if await _gt(_db, tenant.id, template_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request,
        action="template.read",
        resource_type="template",
        resource_id=str(template_id),
    )
    return await _templates(request).get(tenant.id, template_id)


@router.get("/v1/apipi/templates/{template_id}/download")
async def download_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Response:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_template as _gt

        if await _gt(_db, tenant.id, template_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request,
        action="template.read",
        resource_type="template",
        resource_id=str(template_id),
    )
    filename, payload = await _templates(request).download(tenant.id, template_id)
    if isinstance(payload, str):
        return RedirectResponse(payload, status_code=302)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.delete("/v1/apipi/templates/{template_id}")
async def delete_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_template as _gt

        if await _gt(_db, tenant.id, template_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request,
        action="template.write",
        resource_type="template",
        resource_id=str(template_id),
    )
    return await _templates(request).delete(tenant.id, template_id)


@router.post("/v1/apipi/templates/{template_id}/agents")
async def create_agent_from_template(
    template_id: str,
    body: TemplateAgentCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_template as _gt

        if await _gt(_db, tenant.id, template_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="agent.write", resource_type="agent", resource_id=None
    )
    return await _templates(request).create_agent(
        tenant.id,
        template_id,
        body,
        api_key=await model_key(request),
        user_id=_user_id(request),
        org_id=_org_id(request),
    )


@router.get("/v1/apipi/agents/{agent_id}/export")
async def export_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Response:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_agent as _ga

        if await _ga(_db, tenant.id, agent_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="agent.read", resource_type="agent", resource_id=str(agent_id)
    )
    filename, data = await _templates(request).export_agent(
        tenant.id, agent_id, user_id=_user_id(request)
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )
