import contextlib
import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from apipi.common.errors import ApiError
from apipi.common.objects import NS_FILES
from apipi.config import Settings
from apipi.env.setup import SetupError, file_id_refs_from
from apipi.gateway.auth import not_found
from apipi.gateway.content import ImagePart, image_mimes
from apipi.gateway.errors import not_implemented
from apipi.store.blobs import ObjectStore, file_object_id
from apipi.store.engine import Store
from apipi.store.models import FileRow, SessionFileRow, utc_now
from apipi.store.repo import (
    bind_session_file,
    create_file,
    delete_file,
    delete_unbound_attachments,
    file_cursor,
    get_file,
    list_files,
    list_session_files,
    session_file_cursor,
)

FILE_PURPOSES = frozenset({"user_data", "assistants", "vision"})
DOCUMENT_PURPOSES = frozenset({"user_data", "assistants"})
FILE_KINDS = ("file", "attachment", "image")
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 20
SWEEP_BATCH = 500


def new_file_id() -> str:
    return f"file-{uuid.uuid4().hex}"


def file_body(row: FileRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "object": "file",
        "bytes": row.size,
        "created_at": int(row.created_at.timestamp()),
        "filename": row.filename,
        "purpose": row.purpose,
        "status": "processed",
    }


def apipi_file_body(row: FileRow) -> dict[str, Any]:
    return {
        **file_body(row),
        "kind": row.kind,
        "user_id": row.user_id,
        "content_type": row.content_type,
    }


def session_file_body(binding: SessionFileRow, row: FileRow) -> dict[str, Any]:
    return {
        "file_id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "bytes": row.size,
        "content_type": row.content_type,
        "path": binding.path,
        "item_id": str(binding.item_id) if binding.item_id is not None else None,
        "created_at": int(binding.created_at.timestamp()),
    }


def check_image(settings: Settings, content_type: str | None, size: int) -> str:
    """Check an image upload and return its mime type.

    The type must be in `APIPI_IMAGE_MIMES` and the size within
    `APIPI_MAX_IMAGE_BYTES`.
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in image_mimes(settings):
        raise ApiError(
            "invalid_request",
            f"content_type {mime or 'unknown'} is not an allowed image type",
            code="invalid_request",
        )
    if size > settings.max_image_bytes:
        raise ApiError(
            "invalid_request",
            "Image too large",
            code="payload_too_large",
            status_code=413,
        )
    return mime


def check_page(limit: int, order: str) -> None:
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ApiError(
            "invalid_request",
            f"limit must be between 1 and {MAX_LIST_LIMIT}",
            code="invalid_request",
        )
    if order not in ("asc", "desc"):
        raise ApiError(
            "invalid_request", "order must be asc or desc", code="invalid_request"
        )


def _unknown_after(after: str) -> None:
    raise ApiError(
        "invalid_request",
        f"after {after} is not a file of this list",
        code="invalid_request",
    )


def page_body(data: list[dict[str, Any]], has_more: bool, key: str) -> dict[str, Any]:
    return {
        "object": "list",
        "data": data,
        "first_id": data[0][key] if data else None,
        "last_id": data[-1][key] if data else None,
        "has_more": has_more,
    }


class FileService:
    def __init__(self, store: Store, objects: ObjectStore, settings: Settings) -> None:
        self.store = store
        self.objects = objects
        self.settings = settings

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        data: bytes,
        filename: str,
        purpose: str,
        content_type: str | None = None,
        kind: str = "file",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if purpose not in FILE_PURPOSES:
            not_implemented(purpose, f"purpose {purpose} is not implemented")
        if len(data) > int(self.settings.max_file_bytes):
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        if purpose == "vision":
            check_image(self.settings, content_type, len(data))
            kind = "image"
        name = filename.strip() or "upload"
        file_id = new_file_id()
        await self.objects.put(
            NS_FILES,
            file_object_id(tenant_id, file_id),
            data,
            content_type=content_type,
        )
        async with self.store.session() as db:
            row = await create_file(
                db,
                tenant_id,
                file_id=file_id,
                filename=name,
                purpose=purpose,
                size=len(data),
                content_type=content_type,
                kind=kind,
                user_id=user_id,
            )
            return file_body(row)

    async def image_files(
        self,
        tenant_id: uuid.UUID,
        images: tuple[ImagePart, ...],
        *,
        user_id: str | None = None,
    ) -> dict[str, tuple[str, int]]:
        """Check the `file_id` images and return `{file_id: (mime, size)}`.

        Each must be a file of the tenant that the caller with `user_id`
        can see, with an allowed image type within the image size limit.
        """
        mimes = image_mimes(self.settings)
        known: dict[str, tuple[str, int]] = {}
        async with self.store.session() as db:
            for image in images:
                if not image.file_id or image.file_id in known:
                    continue
                row = await get_file(db, tenant_id, image.file_id, user_id=user_id)
                if row is None:
                    not_found()
                mime = (row.content_type or "").split(";", 1)[0].strip().lower()
                if mime not in mimes:
                    raise ApiError(
                        "invalid_request",
                        f"input_image file {image.file_id} is not an allowed image",
                        code="invalid_request",
                    )
                if row.size > self.settings.max_image_bytes:
                    raise ApiError(
                        "invalid_request",
                        "input_image is too large",
                        code="payload_too_large",
                        status_code=413,
                    )
                known[image.file_id] = (mime, row.size)
        return known

    async def input_images(
        self,
        tenant_id: uuid.UUID,
        images: tuple[ImagePart, ...],
        known: dict[str, tuple[str, int]],
        *,
        user_id: str | None = None,
    ) -> list[tuple[str, str, int]]:
        """Return `(file_id, mime, size)` per image.

        Data URL images are stored as files of kind `image` owned by
        `user_id`, the user of the session.
        """
        out: list[tuple[str, str, int]] = []
        for image in images:
            if image.file_id:
                mime, size = known[image.file_id]
                out.append((image.file_id, mime, size))
                continue
            created = await self.create(
                tenant_id,
                data=image.data,
                filename="image",
                purpose="user_data",
                content_type=image.mime,
                kind="image",
                user_id=user_id,
            )
            out.append((str(created["id"]), image.mime, len(image.data)))
        return out

    async def list_objects(
        self,
        tenant_id: uuid.UUID,
        *,
        kinds: Collection[str] | None = ("file",),
        purpose: str | None = None,
        owner_id: str | None = None,
        session_id: uuid.UUID | None = None,
        filename_prefix: str | None = None,
        ids: Collection[str] | None = None,
        after: str | None = None,
        order: str = "desc",
        limit: int = DEFAULT_LIST_LIMIT,
        apipi: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of the files the caller with `user_id` can see.

        `ids` is the authorization filter and `owner_id` filters by the
        file's `user_id`. `kinds` of None lists every kind. `apipi` adds
        `kind`, `user_id`, and `content_type` to each object.
        """
        check_page(limit, order)
        if kinds is not None:
            unknown = sorted(set(kinds) - set(FILE_KINDS))
            if unknown:
                raise ApiError(
                    "invalid_request",
                    f"kind {unknown[0]} is not file, attachment, or image",
                    code="invalid_request",
                )
        async with self.store.session() as db:
            cursor: tuple[datetime, str] | None = None
            if after is not None:
                cursor = await file_cursor(db, tenant_id, after, user_id=user_id)
                if cursor is None or (ids is not None and after not in ids):
                    _unknown_after(after)
            rows, has_more = await list_files(
                db,
                tenant_id,
                kinds=kinds,
                purpose=purpose,
                owner_id=owner_id,
                session_id=session_id,
                filename_prefix=filename_prefix,
                ids=ids,
                after=cursor,
                order=order,
                limit=limit,
                user_id=user_id,
            )
        body = apipi_file_body if apipi else file_body
        return page_body([body(row) for row in rows], has_more, "id")

    async def list_session_files(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        ids: Collection[str] | None = None,
        after: str | None = None,
        order: str = "desc",
        limit: int = DEFAULT_LIST_LIMIT,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of the files bound to a session the caller has checked."""
        check_page(limit, order)
        async with self.store.session() as db:
            cursor: tuple[datetime, str] | None = None
            if after is not None:
                cursor = await session_file_cursor(
                    db, tenant_id, session_id, after, user_id=user_id
                )
                if cursor is None or (ids is not None and after not in ids):
                    _unknown_after(after)
            rows, has_more = await list_session_files(
                db,
                tenant_id,
                session_id,
                ids=ids,
                after=cursor,
                order=order,
                limit=limit,
                user_id=user_id,
            )
        data = [session_file_body(binding, row) for binding, row in rows]
        return page_body(data, has_more, "file_id")

    async def bind_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        file_ids: Collection[str],
        *,
        path: str | None = None,
        item_id: uuid.UUID | None = None,
    ) -> None:
        """Bind files of the tenant to a session.

        A file already bound to the session keeps its binding. `path` is
        the workspace path of an attachment.
        """
        async with self.store.session() as db:
            for file_id in dict.fromkeys(file_ids):
                await bind_session_file(
                    db, tenant_id, session_id, file_id, path=path, item_id=item_id
                )

    async def bound_files(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[tuple[SessionFileRow, FileRow]]:
        """Every file bound to a session with its binding, oldest first."""
        async with self.store.session() as db:
            rows, _more = await list_session_files(
                db, tenant_id, session_id, order="asc"
            )
        return rows

    async def delete_bytes(self, tenant_id: uuid.UUID, file_ids: list[str]) -> None:
        """Delete stored file bytes after their rows are gone, best effort."""
        for file_id in file_ids:
            with contextlib.suppress(Exception):
                await self.objects.delete(NS_FILES, file_object_id(tenant_id, file_id))

    async def sweep_attachments(self, now: datetime | None = None) -> int:
        """Delete unbound attachments older than `APIPI_ATTACHMENT_TTL`.

        Returns the number of deleted files.
        """
        before = (now if now is not None else utc_now()) - self.settings.attachment_ttl
        total = 0
        while True:
            async with self.store.session() as db:
                deleted = await delete_unbound_attachments(
                    db, before=before, limit=SWEEP_BATCH
                )
            for tenant_id, file_id in deleted:
                await self.delete_bytes(tenant_id, [file_id])
            total += len(deleted)
            if len(deleted) < SWEEP_BATCH:
                return total

    async def meta(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> tuple[str, str | None]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        return row.filename, row.content_type

    async def get(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        return file_body(row)

    async def content(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> tuple[bytes, str | None, str]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        data = await self.objects.get(NS_FILES, file_object_id(tenant_id, file_id))
        if data is None:
            not_found()
        return data, row.content_type, row.filename

    async def delete(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_file(db, tenant_id, file_id, user_id=user_id):
                not_found()
        await self.objects.delete(NS_FILES, file_object_id(tenant_id, file_id))
        return {"id": file_id, "object": "file", "deleted": True}

    async def workspace_files(
        self,
        tenant_id: uuid.UUID,
        environment: dict[str, Any],
        *,
        user_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        try:
            refs = file_id_refs_from(environment)
        except SetupError as exc:
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        if not refs:
            return []
        files: list[tuple[str, bytes]] = []
        async with self.store.session() as db:
            for path, file_id in refs:
                row = await get_file(db, tenant_id, file_id, user_id=user_id)
                if row is None:
                    not_found()
                data = await self.objects.get(
                    NS_FILES, file_object_id(tenant_id, file_id)
                )
                if data is None:
                    not_found()
                files.append((path, data))
        return files
