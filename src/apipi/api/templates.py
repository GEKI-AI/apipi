import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from apipi.api.deps import model_key
from apipi.gateway.auth import require_tenant
from apipi.services.templates import TemplateAgentCreate, TemplateCreate
from apipi.store.disposition import content_disposition
from apipi.store.models import Tenant

router = APIRouter()


def _templates(request: Request) -> Any:
    return request.app.state.gateway.templates


def _user_id(request: Request) -> str | None:
    value = getattr(request.state, "user_id", None)
    return value if isinstance(value, str) and value else None


@router.post("/v1/templates")
async def create_template(
    body: TemplateCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _templates(request).create_from_agent(
        tenant.id, body, created_by=_user_id(request)
    )


@router.post("/v1/templates/import")
async def import_template(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    bundle: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    data = await bundle.read()
    return await _templates(request).import_bundle(
        tenant.id,
        data,
        name=name,
        description=description,
        created_by=_user_id(request),
    )


@router.get("/v1/templates")
async def list_templates(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _templates(request).list_objects(tenant.id)


@router.get("/v1/templates/{template_id}")
async def read_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _templates(request).get(tenant.id, template_id)


@router.get("/v1/templates/{template_id}/download")
async def download_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Response:
    filename, payload = await _templates(request).download(tenant.id, template_id)
    if isinstance(payload, str):
        return RedirectResponse(payload, status_code=302)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@router.delete("/v1/templates/{template_id}")
async def delete_template(
    template_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _templates(request).delete(tenant.id, template_id)


@router.post("/v1/templates/{template_id}/agents")
async def create_agent_from_template(
    template_id: str,
    body: TemplateAgentCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _templates(request).create_agent(
        tenant.id, template_id, body, api_key=model_key(request)
    )


@router.get("/v1/agents/{agent_id}/export")
async def export_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    version: str | None = None,
) -> Response:
    filename, data = await _templates(request).export_agent(
        tenant.id, agent_id, version=version
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition(filename)},
    )
