from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from apipi.gateway.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


def _files(request: Request) -> Any:
    return request.app.state.gateway.files


@router.post("/v1/files")
async def upload_file(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()],
) -> dict[str, Any]:
    data = await file.read()
    filename = file.filename or "upload"
    content_type = file.content_type
    return await _files(request).create(
        tenant.id,
        data=data,
        filename=filename,
        purpose=purpose,
        content_type=content_type,
    )


@router.get("/v1/files")
async def list_files(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _files(request).list_objects(tenant.id)


@router.get("/v1/files/{file_id}")
async def read_file(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _files(request).get(tenant.id, file_id)


@router.get("/v1/files/{file_id}/content")
async def read_file_content(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Response:
    data, content_type, filename = await _files(request).content(tenant.id, file_id)
    media = content_type if content_type else "application/octet-stream"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/v1/files/{file_id}")
async def remove_file(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _files(request).delete(tenant.id, file_id)
