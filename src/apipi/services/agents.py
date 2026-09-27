import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from apipi.config import Settings
from apipi.env.spec import EnvironmentSpec
from apipi.gateway.auth import not_found
from apipi.gateway.schemas import StrictModel
from apipi.services.chat_tools import is_chat_profile, reject_disallowed_chat_tools
from apipi.services.session_defaults import (
    mirror_sandbox_metadata,
    normalize_sandbox_aliases,
    require_default_refs,
    strip_sandbox_metadata,
    validate_defaults_shape,
)
from apipi.store.engine import Store
from apipi.store.models import Agent
from apipi.store.repo import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    update_agent,
)
from apipi.worker.pi.idle import normalize_idle_ttl, validate_idle_metadata
from apipi.worker.pi.model_host import require_saved_model
from apipi.worker.pi.sandbox import validate_sandbox_metadata
from apipi.worker.pi.settings_json import validate_pi_metadata

_UNIMPLEMENTED = ("multi_agent", "tool_search", "programmatic_tool_calling")


class FunctionTool(StrictModel):
    type: Literal["function"]
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class McpHttpTransport(StrictModel):
    type: Literal["http"]
    server_url: str


class McpStdioTransport(StrictModel):
    type: Literal["stdio"]
    command: str
    args: list[str] | None = None
    cwd: str | None = None


class McpTool(StrictModel):
    type: Literal["mcp"]
    server_label: str
    transport: Annotated[
        McpHttpTransport | McpStdioTransport, Field(discriminator="type")
    ]
    headers: dict[str, str] | None = None
    required: bool | None = None
    credential_id: str | None = None
    connection_origin: Literal["service", "environment"] | None = None

    @model_validator(mode="after")
    def origin_supported(self) -> Self:
        if self.connection_origin == "environment":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "connection_origin"},
            )
        return self


AgentTool = Annotated[FunctionTool | McpTool, Field(discriminator="type")]


class SessionDefaults(StrictModel):
    environment: EnvironmentSpec | None = None
    vault_ids: list[uuid.UUID] | None = None


class AgentWrite(StrictModel):
    name: str | None = None
    model: str | None = None
    instructions: str | None = None
    idle_ttl: str | None = None
    metadata: dict[str, Any] | None = None
    tools: list[AgentTool] | None = None
    session_defaults: SessionDefaults | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in _UNIMPLEMENTED:
                if field in data:
                    raise PydanticCustomError(
                        "not_implemented",
                        "{field} is not implemented",
                        {"field": field},
                    )
        return data


def agent_body(agent: Agent) -> dict[str, Any]:
    raw = agent.session_defaults
    defaults = raw if isinstance(raw, dict) else None
    return {
        "id": str(agent.id),
        "name": agent.name,
        "model": agent.model,
        "instructions": agent.instructions,
        "idle_ttl": agent.idle_ttl,
        "metadata": mirror_sandbox_metadata(agent.metadata_json, defaults),
        "tools": agent.tools,
        "session_defaults": defaults,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


def write_payload(body: AgentWrite) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    if "tools" in payload and body.tools is not None:
        payload["tools"] = [tool.model_dump(exclude_none=True) for tool in body.tools]
    if "session_defaults" in payload and body.session_defaults is not None:
        payload["session_defaults"] = body.session_defaults.model_dump(
            mode="json", exclude_none=True
        )
    return payload


class AgentService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def create(
        self,
        tenant_id: uuid.UUID,
        body: AgentWrite,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        payload = write_payload(body)
        if "idle_ttl" in payload:
            payload["idle_ttl"] = normalize_idle_ttl(payload.get("idle_ttl"))
        self._normalize_defaults(
            payload, existing_metadata=None, existing_defaults=None
        )
        validate_pi_metadata(payload.get("metadata"))
        validate_idle_metadata(payload.get("metadata"))
        validate_sandbox_metadata(self.settings, payload.get("metadata"))
        validate_defaults_shape(self.settings, payload.get("session_defaults"))
        if is_chat_profile(payload.get("metadata")):
            reject_disallowed_chat_tools(payload.get("tools"))
        await require_saved_model(self.settings, payload.get("model"), api_key)
        async with self.store.session() as db:
            await require_default_refs(db, tenant_id, payload.get("session_defaults"))
            agent = await create_agent(
                db,
                tenant_id,
                name=payload.get("name"),
                model=payload.get("model"),
                instructions=payload.get("instructions"),
                idle_ttl=payload.get("idle_ttl"),
                metadata=payload.get("metadata"),
                tools=payload.get("tools"),
                session_defaults=payload.get("session_defaults"),
            )
            return agent_body(agent)

    async def list(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            agents = await list_agents(db, tenant_id)
            return {"data": [agent_body(agent) for agent in agents]}

    async def get(self, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            agent = await get_agent(db, tenant_id, agent_id)
            if agent is None:
                not_found()
            return agent_body(agent)

    async def update(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        body: AgentWrite,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        payload = write_payload(body)
        if "model" in payload:
            await require_saved_model(self.settings, payload.get("model"), api_key)
        async with self.store.session() as db:
            existing = await get_agent(db, tenant_id, agent_id)
            if existing is None:
                not_found()
            if "idle_ttl" in payload:
                payload["idle_ttl"] = normalize_idle_ttl(payload.get("idle_ttl"))
            if (
                "session_defaults" in payload
                and payload["session_defaults"] is None
                and "metadata" not in payload
            ):
                payload["metadata"] = strip_sandbox_metadata(existing.metadata_json)
            self._normalize_defaults(
                payload,
                existing_metadata=existing.metadata_json,
                existing_defaults=existing.session_defaults
                if isinstance(existing.session_defaults, dict)
                else None,
            )
            metadata = payload.get("metadata", existing.metadata_json)
            stored = metadata if isinstance(metadata, dict) else None
            validate_pi_metadata(stored)
            validate_idle_metadata(stored)
            validate_sandbox_metadata(self.settings, stored)
            if "session_defaults" in payload and isinstance(
                payload.get("session_defaults"), dict
            ):
                validate_defaults_shape(self.settings, payload["session_defaults"])
                await require_default_refs(db, tenant_id, payload["session_defaults"])
            tools = payload.get("tools", existing.tools)
            if is_chat_profile(metadata):
                reject_disallowed_chat_tools(tools)
            agent = await update_agent(db, tenant_id, agent_id, changes=payload)
            if agent is None:
                not_found()
            return agent_body(agent)

    def _normalize_defaults(
        self,
        payload: dict[str, Any],
        *,
        existing_metadata: dict[str, Any] | None,
        existing_defaults: dict[str, Any] | None,
    ) -> None:
        if "metadata" not in payload and "session_defaults" not in payload:
            return
        if payload.get("session_defaults") is None and "session_defaults" in payload:
            if "metadata" in payload and isinstance(payload["metadata"], dict):
                meta, defaults = normalize_sandbox_aliases(payload["metadata"], None)
                if defaults is not None:
                    payload["metadata"] = meta
                    payload["session_defaults"] = defaults
            return
        meta_source = payload.get("metadata", existing_metadata)
        def_source = payload.get("session_defaults", existing_defaults)
        meta, defaults = normalize_sandbox_aliases(
            meta_source if isinstance(meta_source, dict) else None,
            def_source if isinstance(def_source, dict) else None,
        )
        if meta is not None:
            payload["metadata"] = meta
        if defaults is not None:
            payload["session_defaults"] = defaults

    async def delete(self, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            deleted = await delete_agent(db, tenant_id, agent_id)
            if not deleted:
                not_found()
        return {"id": str(agent_id), "deleted": True}
