import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from apipi.config import Settings
from apipi.env.setup import SetupError
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.services.files import FILE_PURPOSES, file_body, new_file_id
from apipi.services.skill_store import new_skill_id, skill_body
from apipi.services.skills import inspect_skill_zip
from apipi.store.blobs import (
    NS_FILES,
    NS_SKILLS,
    S3Store,
    file_object_id,
    skill_object_id,
)
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_file,
    create_skill,
    create_upload,
    get_file,
    get_skill,
    get_upload,
)

UploadPurpose = Literal["file", "skill", "attachment"]


def _s3(objects: object) -> S3Store:
    if not isinstance(objects, S3Store):
        raise ApiError(
            "invalid_request",
            "Presigned URLs need APIPI_ARTIFACT_STORE=s3",
            code="presign_unsupported",
        )
    return objects


class UploadService:
    def __init__(self, store: Store, objects: object, settings: Settings) -> None:
        self.store = store
        self.objects = objects
        self.settings = settings

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        purpose: str,
        filename: str,
        content_type: str | None,
        size: int,
        file_purpose: str | None = None,
    ) -> dict[str, Any]:
        kind = _purpose(purpose)
        if size < 1 or size > int(self.settings.max_file_bytes):
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        s3 = _s3(self.objects)
        if (
            kind == "file"
            and file_purpose is not None
            and file_purpose not in FILE_PURPOSES
        ):
            raise ApiError(
                "not_implemented",
                f"purpose {file_purpose} is not implemented",
                code=file_purpose,
            )
        ctype = (content_type or "").strip() or "application/octet-stream"
        name = filename.strip() or "upload"
        object_id = new_file_id() if kind == "file" else new_skill_id()
        namespace = NS_FILES if kind == "file" else NS_SKILLS
        key = (
            file_object_id(tenant_id, object_id)
            if kind == "file"
            else skill_object_id(tenant_id, object_id)
        )
        expires = utc_now() + self.settings.presign_ttl
        url, headers = s3.presign(
            "PUT",
            namespace,
            key,
            expires=self.settings.presign_ttl,
            content_type=ctype,
        )
        async with self.store.session() as db:
            row = await create_upload(
                db,
                tenant_id,
                purpose=kind,
                object_id=object_id,
                filename=name,
                content_type=ctype,
                declared_bytes=size,
                expires_at=expires,
            )
            upload_id = row.id
        return {
            "upload_id": str(upload_id),
            "object_id": object_id,
            "method": "PUT",
            "url": url,
            "headers": headers,
            "expires_at": expires.isoformat(),
            "file_purpose": file_purpose if kind == "file" else None,
        }

    async def complete(
        self,
        tenant_id: uuid.UUID,
        upload_id: uuid.UUID,
        *,
        file_purpose: str | None = None,
    ) -> dict[str, Any]:
        s3 = _s3(self.objects)
        async with self.store.session() as db:
            row = await get_upload(db, tenant_id, upload_id)
            if row is None:
                not_found()
            if row.status == "complete":
                if row.purpose == "file":
                    existing = await get_file(db, tenant_id, row.object_id)
                    if existing is None:
                        not_found()
                    return file_body(existing)
                existing_skill = await get_skill(db, tenant_id, row.object_id)
                if existing_skill is None:
                    not_found()
                return skill_body(existing_skill)
            if utc_now() > _aware(row.expires_at):
                raise ApiError(
                    "invalid_request",
                    "Upload URL expired",
                    code="upload_expired",
                )
            namespace = NS_FILES if row.purpose == "file" else NS_SKILLS
            key = (
                file_object_id(tenant_id, row.object_id)
                if row.purpose == "file"
                else skill_object_id(tenant_id, row.object_id)
            )
            meta = await s3.head(namespace, key)
            if meta is None:
                raise ApiError(
                    "invalid_request",
                    "Object is missing; PUT the presigned URL first",
                    code="upload_incomplete",
                )
            size, _ctype = meta
            if size > int(self.settings.max_file_bytes):
                await s3.delete(namespace, key)
                raise ApiError(
                    "invalid_request",
                    "File too large",
                    code="payload_too_large",
                    status_code=413,
                )
            if row.purpose == "file":
                purpose = file_purpose if file_purpose in FILE_PURPOSES else "user_data"
                created = await create_file(
                    db,
                    tenant_id,
                    file_id=row.object_id,
                    filename=row.filename,
                    purpose=purpose,
                    size=size,
                    content_type=row.content_type,
                )
                row.status = "complete"
                return file_body(created)
            data = await s3.get(namespace, key)
            if data is None:
                raise ApiError(
                    "invalid_request",
                    "Object is missing; PUT the presigned URL first",
                    code="upload_incomplete",
                )
            try:
                name = inspect_skill_zip(data)
            except SetupError as exc:
                await s3.delete(namespace, key)
                raise ApiError(
                    "invalid_request", exc.message, code="invalid_request"
                ) from exc
            created_skill = await create_skill(
                db,
                tenant_id,
                skill_id=row.object_id,
                name=name,
                size=size,
            )
            row.status = "complete"
            return skill_body(created_skill)

    def download(
        self,
        namespace: Literal["artifacts", "files", "skills"],
        object_id: str,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        s3 = _s3(self.objects)
        url, headers = s3.presign(
            "GET",
            namespace,
            object_id,
            expires=self.settings.presign_ttl,
            content_type=content_type,
            filename=filename,
        )
        expires = utc_now() + self.settings.presign_ttl
        return {
            "method": "GET",
            "url": url,
            "headers": headers,
            "expires_at": expires.isoformat(),
        }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _purpose(raw: str) -> Literal["file", "skill"]:
    if raw in {"file", "attachment"}:
        return "file"
    if raw == "skill":
        return "skill"
    raise ApiError(
        "invalid_request",
        "purpose must be file, skill, or attachment",
        code="invalid_request",
    )
