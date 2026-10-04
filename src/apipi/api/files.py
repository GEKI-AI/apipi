import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from apipi.common.objects import NS_FILES
from apipi.gateway.auth import check_authorize, not_found, require_tenant
from apipi.store.blobs import file_object_id
from apipi.store.disposition import content_disposition
from apipi.store.models import Tenant
from apipi.store.repo import get_session

router = APIRouter()


def _files(request: Request) -> Any:
    return request.app.state.gateway.files


def _user_id(request: Request) -> str | None:
    value = getattr(request.state, "user_id", None)
    return value if isinstance(value, str) and value else None


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
    await check_authorize(
        request, action="file.write", resource_type="file", resource_id=None
    )
    return await _files(request).create(
        tenant.id,
        data=data,
        filename=filename,
        purpose=purpose,
        content_type=content_type,
        user_id=_user_id(request),
    )


@router.get("/v1/files")
async def list_files(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    limit: int = 20,
    after: str | None = None,
    order: Literal["asc", "desc"] = "desc",
    purpose: str | None = None,
    include_attachments: bool = False,
) -> dict[str, Any]:
    filt = await check_authorize(
        request, action="file.list", resource_type="file", resource_id=None
    )
    return await _files(request).list_objects(
        tenant.id,
        kinds=None if include_attachments else ("file",),
        purpose=purpose,
        ids=filt.ids if filt is not None else None,
        after=after,
        order=order,
        limit=limit,
    )


@router.get("/v1/apipi/files")
async def list_apipi_files(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    kind: Annotated[list[str] | None, Query()] = None,
    session_id: uuid.UUID | None = None,
    user_id: str | None = None,
    purpose: str | None = None,
    filename: str | None = None,
    limit: int = 20,
    after: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    if session_id is not None:
        async with request.app.state.store.session() as db:
            row = await get_session(
                db, tenant.id, session_id, user_id=_user_id(request)
            )
            if row is None:
                not_found()
    filt = await check_authorize(
        request, action="file.list", resource_type="file", resource_id=None
    )
    return await _files(request).list_objects(
        tenant.id,
        kinds=kind or None,
        purpose=purpose,
        user_id=user_id,
        session_id=session_id,
        filename_prefix=filename,
        ids=filt.ids if filt is not None else None,
        after=after,
        order=order,
        limit=limit,
        apipi=True,
    )


@router.get("/v1/files/{file_id}")
async def read_file(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_file as _gf

        if await _gf(_db, tenant.id, file_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="file.read", resource_type="file", resource_id=str(file_id)
    )
    return await _files(request).get(tenant.id, file_id)


@router.get("/v1/files/{file_id}/content")
async def read_file_content(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Response:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_file as _gf

        if await _gf(_db, tenant.id, file_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="file.read", resource_type="file", resource_id=str(file_id)
    )
    data, content_type, filename = await _files(request).content(tenant.id, file_id)
    media = content_type if content_type else "application/octet-stream"
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/v1/apipi/files/{file_id}/download")
async def download_file(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_file as _gf

        if await _gf(_db, tenant.id, file_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="file.read", resource_type="file", resource_id=str(file_id)
    )
    filename, content_type = await _files(request).meta(tenant.id, file_id)
    return request.app.state.gateway.uploads.download(
        NS_FILES,
        file_object_id(tenant.id, file_id),
        filename=filename,
        content_type=content_type,
    )


@router.delete("/v1/files/{file_id}")
async def remove_file(
    file_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    async with request.app.state.store.session() as _db:
        from apipi.store.repo import get_file as _gf

        if await _gf(_db, tenant.id, file_id) is None:
            from apipi.gateway.auth import not_found as _nf

            _nf()
    await check_authorize(
        request, action="file.write", resource_type="file", resource_id=str(file_id)
    )
    return await _files(request).delete(tenant.id, file_id)
