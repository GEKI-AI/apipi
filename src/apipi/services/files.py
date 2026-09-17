import uuid
from typing import Any

from apipi.config import Settings
from apipi.env.setup import SetupError, file_id_refs_from
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError, not_implemented
from apipi.store.blobs import NS_FILES, ObjectStore, file_object_id
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
