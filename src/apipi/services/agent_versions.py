import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import Settings
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.services.agents import AgentWrite, write_payload
from apipi.services.session_defaults import (
    require_default_refs,
    validate_defaults_shape,
)
from apipi.store.engine import Store
from apipi.store.models import (
    Agent,
    AgentVersion,
    AgentVersionActivation,
    SessionRow,
    Turn,
    utc_now,
)
from apipi.store.repo import get_agent, get_credential_by_id
from apipi.worker.pi.model_host import require_saved_model
from apipi.worker.pi.settings_json import reasoning_body

AGENT_VERSION_KEY = "apipi.agent_version"
VERSIONED_FIELDS = frozenset(
    {
        "name",
        "model",
        "instructions",
        "idle_ttl",
        "metadata",
        "tools",
        "session_defaults",
        "reasoning",
    }
)
NON_VERSIONED_FIELDS = frozenset({"service_tier", "text"})
READY = "ready"


def snapshot_from_agent(agent: Agent) -> dict[str, Any]:
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    tools = agent.tools if isinstance(agent.tools, list) else []
    defaults = (
        agent.session_defaults if isinstance(agent.session_defaults, dict) else None
    )
    return {
        "name": agent.name,
        "model": agent.model,
        "instructions": agent.instructions,
        "idle_ttl": agent.idle_ttl,
        "metadata": metadata,
        "tools": tools,
        "session_defaults": defaults,
        "reasoning": reasoning_body(metadata),
    }


def snapshot_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(
        right, sort_keys=True, default=str
    )


def version_ref(version: AgentVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {"id": str(version.id), "number": version.number}


def version_body(
    version: AgentVersion, *, active_id: uuid.UUID | None
) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "number": version.number,
        "status": version.status,
        "active": version.id == active_id,
        "source": version.source,
        "created_by": version.created_by,
        "note": version.note,
        "created_at": version.created_at.isoformat(),
        "definition": version.definition,
    }


def apply_definition(agent: Agent, definition: dict[str, Any]) -> None:
    agent.name = definition.get("name")
    agent.model = definition.get("model")
    agent.instructions = definition.get("instructions")
    agent.idle_ttl = definition.get("idle_ttl")
    metadata = definition.get("metadata")
    agent.metadata_json = metadata if isinstance(metadata, dict) else {}
    tools = definition.get("tools")
    agent.tools = tools if isinstance(tools, list) else []
    defaults = definition.get("session_defaults")
    agent.session_defaults = defaults if isinstance(defaults, dict) else None
    agent.updated_at = utc_now()


async def load_pinned_version(
    db: AsyncSession, tenant_id: uuid.UUID, row: SessionRow
) -> AgentVersion:
    if row.agent_version_id is not None:
        version = await db.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.id == row.agent_version_id,
            )
        )
        if version is None:
            not_found()
        return version
    if row.agent_id is None:
        not_found()
    agent = await get_agent(db, tenant_id, row.agent_id)
    if agent is None or agent.active_version_id is None:
        not_found()
    version = await db.scalar(
        select(AgentVersion).where(
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.id == agent.active_version_id,
        )
    )
    if version is None:
        not_found()
    return version


def parse_version_ref(value: object) -> tuple[str, uuid.UUID | int]:
    if value == "active":
        raise ApiError(
            "invalid_request",
            "apipi.agent_version active is not implemented",
            code="invalid_request",
        )
    if isinstance(value, bool):
        raise ApiError(
            "invalid_request",
            "apipi.agent_version must be a version number or id",
            code="invalid_request",
        )
    if isinstance(value, int) and value >= 1:
        return "number", value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) >= 1:
            return "number", int(text)
        try:
            return "id", uuid.UUID(text)
        except ValueError:
            pass
    raise ApiError(
        "invalid_request",
        "apipi.agent_version must be a version number or id",
        code="invalid_request",
    )


class AgentVersionService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def record(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        source: str,
        created_by: str | None,
        note: str | None = None,
        activate: bool = True,
        definition: dict[str, Any] | None = None,
    ) -> AgentVersion:
        body = definition if definition is not None else snapshot_from_agent(agent)
        number = await self._next_number(db, agent.tenant_id, agent.id)
        version = AgentVersion(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            number=number,
            status=READY,
            definition=body,
            source=source,
            created_by=created_by,
            note=note,
        )
        db.add(version)
        await db.flush()
        if activate:
            await self._activate_row(
                db, agent, version, activated_by=created_by, apply=False
            )
        await self._prune(db, agent.tenant_id, agent.id, agent.active_version_id)
        return version

    async def create_explicit(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        definition: dict[str, Any] | None,
        note: str | None,
        activate: bool,
        created_by: str | None,
        api_key: str | None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            if definition is None:
                body = snapshot_from_agent(agent)
                source = "explicit"
            else:
                body = await self._validated_definition(
                    db, tenant_id, definition, api_key=api_key
                )
                source = "explicit"
            version = await self.record(
                db,
                agent,
                source=source,
                created_by=created_by,
                note=note,
                activate=False,
                definition=body,
            )
            if activate:
                await self._activate_row(
                    db, agent, version, activated_by=created_by, apply=True
                )
            return version_body(version, active_id=agent.active_version_id)

    async def list_versions(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        limit: int = 20,
        after: str | None = None,
        include_definition: bool = False,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ApiError(
                "invalid_request",
                "limit must be from 1 to 100",
                code="invalid_request",
            )
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            stmt = (
                select(AgentVersion)
                .where(
                    AgentVersion.tenant_id == tenant_id,
                    AgentVersion.agent_id == agent_id,
                )
                .order_by(AgentVersion.number.desc())
            )
            if after:
                cursor = await self._get(db, tenant_id, agent_id, after)
                stmt = stmt.where(AgentVersion.number < cursor.number)
            rows = list(await db.scalars(stmt.limit(limit + 1)))
            more = len(rows) > limit
            page = rows[:limit]
            data = []
            for row in page:
                item = version_body(row, active_id=agent.active_version_id)
                if not include_definition:
                    item.pop("definition", None)
                data.append(item)
            return {
                "data": data,
                "has_more": more,
                "next": str(page[-1].number) if more and page else None,
            }

    async def get_version(
        self, tenant_id: uuid.UUID, agent_id: uuid.UUID, ref: str
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._get(db, tenant_id, agent_id, ref)
            return version_body(version, active_id=agent.active_version_id)

    async def activate(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        ref: str,
        *,
        activated_by: str | None,
        api_key: str | None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._get(db, tenant_id, agent_id, ref)
            if version.status != READY:
                raise ApiError(
                    "invalid_request",
                    "Only a ready version can be activated",
                    code="invalid_request",
                )
            await self._revalidate(db, tenant_id, version.definition, api_key=api_key)
            await self._activate_row(
                db, agent, version, activated_by=activated_by, apply=True
            )
            return version_body(version, active_id=agent.active_version_id)

    async def delete_version(
        self, tenant_id: uuid.UUID, agent_id: uuid.UUID, ref: str
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._get(db, tenant_id, agent_id, ref)
            if agent.active_version_id == version.id:
                raise ApiError(
                    "invalid_request",
                    "Cannot delete the active version",
                    code="invalid_request",
                )
            if await self._referenced(db, tenant_id, version.id):
                raise ApiError(
                    "invalid_request",
                    "Cannot delete a version that a session or turn still uses",
                    code="invalid_request",
                )
            number = version.number
            await db.delete(version)
            await db.flush()
            return {"id": str(version.id), "number": number, "deleted": True}

    async def active_of(self, db: AsyncSession, agent: Agent) -> AgentVersion | None:
        if agent.active_version_id is None:
            return None
        return await db.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == agent.tenant_id,
                AgentVersion.id == agent.active_version_id,
            )
        )

    async def resolve(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        metadata: dict[str, Any] | None,
    ) -> AgentVersion:
        agent = await get_agent(db, tenant_id, agent_id)
        if agent is None:
            not_found()
        pin = None if metadata is None else metadata.get(AGENT_VERSION_KEY)
        if pin is not None:
            kind, value = parse_version_ref(pin)
            version = await self._lookup(db, tenant_id, agent_id, kind, value)
        elif agent.active_version_id is not None:
            version = await self.active_of(db, agent)
        else:
            version = None
        if version is None:
            not_found()
        return version

    async def load_for_session(
        self, db: AsyncSession, tenant_id: uuid.UUID, row: SessionRow
    ) -> AgentVersion:
        return await load_pinned_version(db, tenant_id, row)

    async def _validated_definition(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        raw: dict[str, Any],
        *,
        api_key: str | None,
    ) -> dict[str, Any]:
        body = AgentWrite.model_validate(raw)
        payload = write_payload(body)
        await self._revalidate(db, tenant_id, payload, api_key=api_key)
        metadata = (
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        )
        return {
            "name": payload.get("name"),
            "model": payload.get("model"),
            "instructions": payload.get("instructions"),
            "idle_ttl": payload.get("idle_ttl"),
            "metadata": metadata,
            "tools": payload.get("tools") or [],
            "session_defaults": payload.get("session_defaults"),
            "reasoning": reasoning_body(metadata),
        }

    async def _revalidate(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        definition: dict[str, Any],
        *,
        api_key: str | None,
    ) -> None:
        model = definition.get("model")
        await require_saved_model(
            self.settings, model if isinstance(model, str) else None, api_key
        )
        defaults = definition.get("session_defaults")
        if isinstance(defaults, dict):
            validate_defaults_shape(self.settings, defaults)
            await require_default_refs(db, tenant_id, defaults)
        tools = definition.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw = tool.get("credential_id")
                if not isinstance(raw, str) or not raw:
                    continue
                try:
                    cred_id = uuid.UUID(raw)
                except ValueError as exc:
                    raise ApiError(
                        "invalid_request",
                        "Version credential_id is not a UUID",
                        code="invalid_request",
                    ) from exc
                if await get_credential_by_id(db, tenant_id, cred_id) is None:
                    raise ApiError(
                        "invalid_request",
                        "Version references a missing credential",
                        code="invalid_request",
                    )

    async def _activate_row(
        self,
        db: AsyncSession,
        agent: Agent,
        version: AgentVersion,
        *,
        activated_by: str | None,
        apply: bool,
    ) -> None:
        previous = agent.active_version_id
        if apply:
            if not isinstance(version.definition, dict):
                raise ApiError(
                    "invalid_request",
                    "Version definition is invalid",
                    code="invalid_request",
                )
            apply_definition(agent, version.definition)
        agent.active_version_id = version.id
        db.add(
            AgentVersionActivation(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                version_id=version.id,
                from_version_id=previous,
                activated_by=activated_by,
            )
        )
        await db.flush()

    async def _next_number(
        self, db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> int:
        current = await db.scalar(
            select(func.max(AgentVersion.number)).where(
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.agent_id == agent_id,
            )
        )
        return int(current or 0) + 1

    async def _get(
        self, db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, ref: str
    ) -> AgentVersion:
        kind, value = parse_version_ref(ref)
        return await self._lookup(db, tenant_id, agent_id, kind, value)

    async def _lookup(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        kind: str,
        value: uuid.UUID | int,
    ) -> AgentVersion:
        stmt = select(AgentVersion).where(
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.agent_id == agent_id,
        )
        if kind == "number":
            stmt = stmt.where(AgentVersion.number == value)
        else:
            stmt = stmt.where(AgentVersion.id == value)
        version = await db.scalar(stmt)
        if version is None:
            not_found()
        return version

    async def _referenced(
        self, db: AsyncSession, tenant_id: uuid.UUID, version_id: uuid.UUID
    ) -> bool:
        session = await db.scalar(
            select(SessionRow.id).where(
                SessionRow.tenant_id == tenant_id,
                SessionRow.agent_version_id == version_id,
            )
        )
        if session is not None:
            return True
        turn = await db.scalar(
            select(Turn.id).where(
                Turn.tenant_id == tenant_id,
                Turn.agent_version_id == version_id,
            )
        )
        return turn is not None

    async def _prune(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        active_id: uuid.UUID | None,
    ) -> None:
        keep = self.settings.agent_versions_keep
        if keep is None:
            return
        rows = list(
            await db.scalars(
                select(AgentVersion)
                .where(
                    AgentVersion.tenant_id == tenant_id,
                    AgentVersion.agent_id == agent_id,
                )
                .order_by(AgentVersion.number.asc())
            )
        )
        extra = len(rows) - keep
        if extra <= 0:
            return
        for version in rows:
            if extra <= 0:
                return
            if version.id == active_id:
                continue
            if await self._referenced(db, tenant_id, version.id):
                continue
            await db.delete(version)
            extra -= 1
        await db.flush()
