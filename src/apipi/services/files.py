import uuid
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
from apipi.store.models import FileRow
from apipi.store.repo import create_file, delete_file, get_file, list_files

FILE_PURPOSES = frozenset({"user_data", "assistants"})


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
            )
            return file_body(row)

    async def image_files(
        self, tenant_id: uuid.UUID, images: tuple[ImagePart, ...]
    ) -> dict[str, tuple[str, int]]:
        """Check the `file_id` images and return `{file_id: (mime, size)}`.

        Each must be a file of the tenant with an allowed image type
        within the image size limit.
        """
        mimes = image_mimes(self.settings)
        known: dict[str, tuple[str, int]] = {}
        async with self.store.session() as db:
            for image in images:
                if not image.file_id or image.file_id in known:
                    continue
                row = await get_file(db, tenant_id, image.file_id)
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
    ) -> list[tuple[str, str, int]]:
        """Return `(file_id, mime, size)` per image, storing data URL images."""
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
            )
            out.append((str(created["id"]), image.mime, len(image.data)))
        return out

    async def list_objects(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_files(db, tenant_id)
        data = [file_body(row) for row in rows]
        return {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": False,
        }

    async def meta(self, tenant_id: uuid.UUID, file_id: str) -> tuple[str, str | None]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id)
        if row is None:
            not_found()
        return row.filename, row.content_type

    async def get(self, tenant_id: uuid.UUID, file_id: str) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id)
        if row is None:
            not_found()
        return file_body(row)

    async def content(
        self, tenant_id: uuid.UUID, file_id: str
    ) -> tuple[bytes, str | None, str]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id)
        if row is None:
            not_found()
        data = await self.objects.get(NS_FILES, file_object_id(tenant_id, file_id))
        if data is None:
            not_found()
        return data, row.content_type, row.filename

    async def delete(self, tenant_id: uuid.UUID, file_id: str) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_file(db, tenant_id, file_id):
                not_found()
        await self.objects.delete(NS_FILES, file_object_id(tenant_id, file_id))
        return {"id": file_id, "object": "file", "deleted": True}

    async def workspace_files(
        self, tenant_id: uuid.UUID, environment: dict[str, Any]
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
                row = await get_file(db, tenant_id, file_id)
                if row is None:
                    not_found()
                data = await self.objects.get(
                    NS_FILES, file_object_id(tenant_id, file_id)
                )
                if data is None:
                    not_found()
                files.append((path, data))
        return files
