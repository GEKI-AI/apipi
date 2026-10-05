import asyncio
import contextlib
import uuid
import weakref
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.errors import ApiError
from apipi.common.objects import NS_FILES, NS_SKILLS, NS_UPLOADS, Namespace
from apipi.common.skills import inspect_skill_zip
from apipi.config import Settings
from apipi.env.setup import SetupError
from apipi.gateway.auth import not_found
from apipi.services.files import (
    DOCUMENT_PURPOSES,
    check_image,
    file_body,
    new_file_id,
)
from apipi.services.skill_store import new_skill_id, skill_body
from apipi.store.blobs import (
    S3Store,
    file_object_id,
    get_sized,
    skill_object_id,
    upload_object_id,
)
from apipi.store.engine import Store
from apipi.store.models import UploadRow, utc_now
from apipi.store.repo import (
    create_file,
    create_skill,
    create_upload,
    get_file,
    get_skill,
    get_upload,
)

UploadPurpose = Literal["file", "skill", "attachment", "image"]


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
        self._locks: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def _lock(self, upload_id: uuid.UUID) -> asyncio.Lock:
        lock = self._locks.get(upload_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[upload_id] = lock
        return lock

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        purpose: str,
        filename: str,
        content_type: str | None,
        size: int,
        file_purpose: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        kind = _purpose(purpose)
        is_file = kind != "skill"
        if size < 1 or size > int(self.settings.max_file_bytes):
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        s3 = _s3(self.objects)
        if is_file:
            _file_purpose(kind, file_purpose)
        if kind == "image":
            check_image(self.settings, content_type, size)
            file_purpose = "vision"
        ctype = (content_type or "").strip() or "application/octet-stream"
        name = filename.strip() or "upload"
        object_id = new_file_id() if is_file else new_skill_id()
        upload_id = uuid.uuid4()
        expires = utc_now() + self.settings.presign_ttl
        url, headers = s3.presign(
            "PUT",
            NS_UPLOADS,
            upload_object_id(tenant_id, upload_id),
            expires=self.settings.presign_ttl,
            content_type=ctype,
            size=size,
        )
        async with self.store.session() as db:
            await create_upload(
                db,
                tenant_id,
                upload_id=upload_id,
                purpose=kind,
                object_id=object_id,
                filename=name,
                content_type=ctype,
                declared_bytes=size,
                expires_at=expires,
                user_id=user_id,
            )
        return {
            "upload_id": str(upload_id),
            "object_id": object_id,
            "method": "PUT",
            "url": url,
            "headers": headers,
            "expires_at": expires.isoformat(),
            "file_purpose": file_purpose if is_file else None,
        }

    async def complete(
        self,
        tenant_id: uuid.UUID,
        upload_id: uuid.UUID,
        *,
        file_purpose: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Complete one upload, once.

        The upload row is locked for the whole complete (`FOR UPDATE` on
        Postgres, and a lock in this process), so a second complete waits
        and then returns the file or skill of the first. When the copy
        succeeded and a later step fails, the copied object is deleted
        again, best effort, but only while the upload is still pending.
        """
        s3 = _s3(self.objects)
        copied: list[tuple[Namespace, str]] = []
        async with self._lock(upload_id):
            try:
                body = await self._complete(
                    s3, tenant_id, upload_id, file_purpose, user_id, copied
                )
            except Exception:
                if copied:
                    with contextlib.suppress(Exception):
                        await self._drop_copies(s3, tenant_id, upload_id, copied)
                raise
        return body

    async def _drop_copies(
        self,
        s3: S3Store,
        tenant_id: uuid.UUID,
        upload_id: uuid.UUID,
        copied: list[tuple[Namespace, str]],
    ) -> None:
        """Delete the copies of a failed complete while the upload is pending.

        The upload row stays locked while the copies are deleted, so a
        complete in another process either copies again afterwards or has
        already committed, and then nothing is deleted.
        """
        async with self.store.session() as db:
            row = await get_upload(db, tenant_id, upload_id, for_update=True)
            if row is None or row.status != "pending":
                return
            for namespace, key in copied:
                await s3.delete(namespace, key)

    async def _complete(
        self,
        s3: S3Store,
        tenant_id: uuid.UUID,
        upload_id: uuid.UUID,
        file_purpose: str | None,
        user_id: str | None,
        copied: list[tuple[Namespace, str]],
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_upload(
                db, tenant_id, upload_id, user_id=user_id, for_update=True
            )
            if row is None:
                not_found()
            is_file = row.purpose != "skill"
            purpose = _file_purpose(row.purpose, file_purpose) if is_file else ""
            if row.status == "complete":
                if is_file:
                    existing = await get_file(
                        db, tenant_id, row.object_id, user_id=user_id
                    )
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
            body = await self._finish(db, s3, tenant_id, row, purpose, copied)
        copied.clear()
        with contextlib.suppress(Exception):
            await s3.delete(NS_UPLOADS, upload_object_id(tenant_id, upload_id))
        return body

    async def _finish(
        self,
        db: AsyncSession,
        s3: S3Store,
        tenant_id: uuid.UUID,
        row: UploadRow,
        purpose: str,
        copied: list[tuple[Namespace, str]],
    ) -> dict[str, Any]:
        """Check the uploaded object, copy it to its final key, and store the row.

        The final key never has a PUT URL, so the bytes of the file or
        skill cannot change after complete.
        """
        is_file = row.purpose != "skill"
        upload_key = upload_object_id(tenant_id, row.id)
        meta = await s3.head(NS_UPLOADS, upload_key)
        if meta is None:
            raise ApiError(
                "invalid_request",
                "Object is missing; PUT the presigned URL first",
                code="upload_incomplete",
            )
        if meta.size > min(int(self.settings.max_file_bytes), row.declared_bytes):
            await s3.delete(NS_UPLOADS, upload_key)
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        if row.purpose == "image":
            try:
                check_image(self.settings, row.content_type, meta.size)
            except ApiError:
                await s3.delete(NS_UPLOADS, upload_key)
                raise
        namespace = NS_FILES if is_file else NS_SKILLS
        key = (
            file_object_id(tenant_id, row.object_id)
            if is_file
            else skill_object_id(tenant_id, row.object_id)
        )
        await s3.copy(NS_UPLOADS, upload_key, namespace, key, etag=meta.etag)
        copied.append((namespace, key))
        if is_file:
            created = await create_file(
                db,
                tenant_id,
                file_id=row.object_id,
                filename=row.filename,
                purpose=purpose,
                size=meta.size,
                content_type=row.content_type,
                kind=row.purpose,
                user_id=row.user_id,
            )
            row.status = "complete"
            return file_body(created)
        data = await get_sized(s3, namespace, key, meta.size)
        if data is None:
            raise ApiError(
                "invalid_request",
                "Object is missing; PUT the presigned URL first",
                code="upload_incomplete",
            )
        try:
            name = inspect_skill_zip(data)
        except SetupError as exc:
            await s3.delete(NS_UPLOADS, upload_key)
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        created_skill = await create_skill(
            db,
            tenant_id,
            skill_id=row.object_id,
            name=name,
            size=meta.size,
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


def _file_purpose(kind: str, file_purpose: str | None) -> str:
    """The Files API purpose of a file upload, or an error for a wrong one."""
    if kind == "image":
        if file_purpose not in (None, "vision"):
            raise ApiError(
                "invalid_request",
                "purpose image takes file_purpose vision",
                code="invalid_request",
            )
        return "vision"
    if file_purpose == "vision":
        raise ApiError(
            "invalid_request",
            "file_purpose vision needs purpose image",
            code="invalid_request",
        )
    if file_purpose is None:
        return "user_data"
    if file_purpose not in DOCUMENT_PURPOSES:
        raise ApiError(
            "not_implemented",
            f"purpose {file_purpose} is not implemented",
            code=file_purpose,
        )
    return file_purpose


def _purpose(raw: str) -> UploadPurpose:
    if raw == "file":
        return "file"
    if raw == "attachment":
        return "attachment"
    if raw == "image":
        return "image"
    if raw == "skill":
        return "skill"
    raise ApiError(
        "invalid_request",
        "purpose must be file, attachment, image, or skill",
        code="invalid_request",
    )
