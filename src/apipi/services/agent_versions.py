import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from apipi.config import Settings
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.services.agents import agent_body
from apipi.services.session_defaults import (
    require_default_refs,
    validate_defaults_shape,
)
from apipi.store.engine import Store
from apipi.store.models import Agent, AgentVersion, SessionRow
from apipi.store.repo import get_agent, get_credential_by_id
from apipi.worker.pi.model_host import require_saved_model
from apipi.worker.pi.settings_json import reasoning_body

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


def version_ref(version: AgentVersion) -> dict[str, Any]:
    return {"id": str(version.id), "number": version.number}


def version_body(
    version: AgentVersion, *, include_definition: bool = True
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(version.id),
        "number": version.number,
        "name": version.name,
        "comment": version.comment,
        "source": version.source,
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat(),
    }
    if include_definition:
        body["definition"] = version.definition
    return body


def apply_definition(agent: Agent, definition: dict[str, Any]) -> None:
    from apipi.store.models import utc_now

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


async def definition_for_session(
    db: AsyncSession, tenant_id: uuid.UUID, row: SessionRow
) -> dict[str, Any] | None:
    if row.agent_id is None:
        return None
    agent = await get_agent(db, tenant_id, row.agent_id)
    if agent is None:
        return None
    return snapshot_from_agent(agent)


def parse_version_ref(value: object) -> tuple[str, uuid.UUID | int]:
    if isinstance(value, bool):
        raise ApiError(
            "invalid_request",
            "Version must be a number or id",
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
        "Version must be a number or id",
        code="invalid_request",
    )


class AgentVersionService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def create_explicit(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        name: str | None,
        comment: str | None,
        created_by: str | None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._snapshot(
                db,
                agent,
                source="explicit",
                name=name,
                comment=comment,
                created_by=created_by,
            )
            await self._prune(db, tenant_id, agent_id)
            return version_body(version)

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
            return {
                "data": [
                    version_body(row, include_definition=include_definition)
                    for row in page
                ],
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
            return version_body(version)

    async def restore(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        ref: str,
        *,
        created_by: str | None,
        api_key: str | None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._get(db, tenant_id, agent_id, ref)
            definition = version.definition
            if not isinstance(definition, dict):
                raise ApiError(
                    "invalid_request",
                    "Version definition is invalid",
                    code="invalid_request",
                )
            await self._revalidate(db, tenant_id, definition, api_key=api_key)
            pre = await self._snapshot(
                db,
                agent,
                source="pre_restore",
                name=None,
                comment=f"before restore of v{version.number}",
                created_by=created_by,
            )
            apply_definition(agent, definition)
            agent.revision = (agent.revision or 0) + 1
            await db.flush()
            await self._prune(db, tenant_id, agent_id)
            body = agent_body(agent)
            body["pre_restore_version"] = version_ref(pre)
            return body

    async def delete_version(
        self, tenant_id: uuid.UUID, agent_id: uuid.UUID, ref: str
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            version = await self._get(db, tenant_id, agent_id, ref)
            number = version.number
            version_id = version.id
            await db.delete(version)
            await db.flush()
            return {"id": str(version_id), "number": number, "deleted": True}

    async def _snapshot(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        source: str,
        name: str | None,
        comment: str | None,
        created_by: str | None,
    ) -> AgentVersion:
        number = await self._allocate_number(db, agent)
        version = AgentVersion(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            number=number,
            definition=snapshot_from_agent(agent),
            name=name,
            comment=comment,
            source=source,
            created_by=created_by,
        )
        db.add(version)
        await db.flush()
        return version

    async def _allocate_number(self, db: AsyncSession, agent: Agent) -> int:
        result = await db.execute(
            update(Agent)
            .where(Agent.tenant_id == agent.tenant_id, Agent.id == agent.id)
            .values(version_seq=Agent.version_seq + 1)
            .returning(Agent.version_seq)
        )
        number = int(result.scalar_one())
        set_committed_value(agent, "version_seq", number)
        return number

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

    async def _get(
        self, db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, ref: str
    ) -> AgentVersion:
        kind, value = parse_version_ref(ref)
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

    async def _prune(
        self, db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> None:
        keep = self.settings.agent_versions_keep
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
        for version in rows[:extra]:
            await db.delete(version)
        await db.flush()
