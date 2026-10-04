import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.gateway.auth import check_authorize, require_tenant
from apipi.gateway.schemas import StrictModel
from apipi.store.models import Tenant

router = APIRouter()


class UploadCreate(StrictModel):
    purpose: str
    filename: str
    content_type: str | None = None
    bytes: int
    file_purpose: str | None = None


class UploadComplete(StrictModel):
    file_purpose: str | None = None


def _uploads(request: Request) -> Any:
    return request.app.state.gateway.uploads


def _user_id(request: Request) -> str | None:
    value = getattr(request.state, "user_id", None)
    return value if isinstance(value, str) and value else None


@router.post("/v1/apipi/uploads")
async def create_upload(
    body: UploadCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await check_authorize(
        request, action="file.write", resource_type="file", resource_id=None
    )
    return await _uploads(request).create(
        tenant.id,
        purpose=body.purpose,
        filename=body.filename,
        content_type=body.content_type,
        size=body.bytes,
        file_purpose=body.file_purpose,
        user_id=_user_id(request),
    )


@router.post("/v1/apipi/uploads/{upload_id}/complete")
async def complete_upload(
    upload_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    body: UploadComplete | None = None,
) -> dict[str, Any]:
    await check_authorize(
        request, action="file.write", resource_type="file", resource_id=None
    )
    purpose = body.file_purpose if body is not None else None
    return await _uploads(request).complete(tenant.id, upload_id, file_purpose=purpose)
