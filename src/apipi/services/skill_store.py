import uuid
from pathlib import Path
from typing import Any

from apipi.config import Settings
from apipi.env.setup import SetupError, skill_refs_from
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.services.skills import inspect_skill_zip, unpack_skill_zip
from apipi.store.blobs import NS_SKILLS, ObjectStore, skill_object_id
from apipi.store.engine import Store
from apipi.store.models import SkillRow
from apipi.store.repo import create_skill, delete_skill, get_skill, list_skills


def new_skill_id() -> str:
    return f"skill-{uuid.uuid4().hex}"


def skill_body(row: SkillRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "object": "skill",
        "name": row.name,
        "bytes": row.size,
        "created_at": int(row.created_at.timestamp()),
    }


class SkillService:
    def __init__(self, store: Store, objects: ObjectStore, settings: Settings) -> None:
        self.store = store
        self.objects = objects
        self.settings = settings

    async def create(
        self, tenant_id: uuid.UUID, *, data: bytes, filename: str
    ) -> dict[str, Any]:
        if len(data) > int(self.settings.max_file_bytes):
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        try:
            name = inspect_skill_zip(data)
        except SetupError as exc:
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        del filename
        skill_id = new_skill_id()
        await self.objects.put(
            NS_SKILLS,
            skill_object_id(tenant_id, skill_id),
            data,
            content_type="application/zip",
        )
        async with self.store.session() as db:
            row = await create_skill(
                db, tenant_id, skill_id=skill_id, name=name, size=len(data)
            )
            return skill_body(row)

    async def list_objects(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_skills(db, tenant_id)
        data = [skill_body(row) for row in rows]
        return {
            "object": "list",
            "data": data,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
            "has_more": False,
        }

    async def get(self, tenant_id: uuid.UUID, skill_id: str) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_skill(db, tenant_id, skill_id)
        if row is None:
            not_found()
        return skill_body(row)

    async def delete(self, tenant_id: uuid.UUID, skill_id: str) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_skill(db, tenant_id, skill_id):
                not_found()
        await self.objects.delete(NS_SKILLS, skill_object_id(tenant_id, skill_id))
        return {"id": skill_id, "object": "skill", "deleted": True}

    async def install(
        self,
        tenant_id: uuid.UUID,
        environment: dict[str, Any],
        workspace: Path,
    ) -> None:
        try:
            refs = skill_refs_from(environment)
        except SetupError as exc:
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        if not refs:
            return
        max_bytes = int(self.settings.max_workspace_bytes)
        async with self.store.session() as db:
            for skill_id in refs:
                row = await get_skill(db, tenant_id, skill_id)
                if row is None:
                    not_found()
                data = await self.objects.get(
                    NS_SKILLS, skill_object_id(tenant_id, skill_id)
                )
                if data is None:
                    not_found()
                try:
                    unpack_skill_zip(workspace, data, max_bytes=max_bytes)
                except SetupError as exc:
                    raise ApiError(
                        "invalid_request", exc.message, code="invalid_request"
                    ) from exc
