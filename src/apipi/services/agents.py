import re
import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from apipi.config import Settings
from apipi.env.spec import EnvironmentSpec
from apipi.gateway.auth import not_found
from apipi.gateway.schemas import StrictModel
from apipi.services.env_none import (
    is_env_none,
    reject_builtin_tools_for_env_none,
    reject_tools_for_env_none,
)
from apipi.services.search import SearchResolver, require_search
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
from apipi.worker.pi.sandbox import (
    reject_removed_size_key,
    strip_removed_size_key,
    validate_sandbox_metadata,
)
from apipi.worker.pi.settings_json import (
    THINKING_KEY,
    apply_reasoning_effort,
    public_metadata,
    reasoning_body,
    reject_client_thinking_key,
    reject_codemode_without_builtin_tools,
    reject_reasoning_conflict,
    require_thinking_supported,
    thinking_from_metadata,
    validate_pi_metadata,
)

_UNIMPLEMENTED = ("multi_agent", "tool_search", "programmatic_tool_calling")

_SERVER_LABEL = re.compile(r"^[A-Za-z0-9_-]+$")


def _defaults_environment(defaults: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(defaults, dict):
        return None
    env = defaults.get("environment")
    return env if isinstance(env, dict) else None


class FunctionTool(StrictModel):
    type: Literal["function"]
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class McpTool(StrictModel):
    type: Literal["mcp"]
    server_label: str
    server_url: str
    headers: dict[str, str] | None = None
    allowed_tools: list[str] | dict[str, Any] | None = None
    require_approval: str | None = None
    server_description: str | None = None
    required: bool | None = None
    credential_id: str | None = None
    connection_origin: Literal["service", "environment"] | None = None
    connector_id: str | None = None
    authorization: str | None = None

    @model_validator(mode="after")
    def connector_rejected(self) -> Self:
        for field in ("connector_id", "authorization"):
            if getattr(self, field) is not None:
                raise PydanticCustomError(
                    "not_implemented",
                    "{field} is not implemented",
                    {"field": field},
                )
        return self

    @model_validator(mode="after")
    def origin_supported(self) -> Self:
        if self.connection_origin == "environment":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "connection_origin"},
            )
        return self

    @model_validator(mode="after")
    def approval_supported(self) -> Self:
        if self.require_approval not in (None, "never"):
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "require_approval"},
            )
        return self

    @model_validator(mode="after")
    def label_valid(self) -> Self:
        if not _SERVER_LABEL.fullmatch(self.server_label):
            raise PydanticCustomError(
                "invalid_value",
                "{field} is invalid",
                {"field": "server_label"},
            )
        return self

    @model_validator(mode="after")
    def tools_supported(self) -> Self:
        allowed = self.allowed_tools
        if allowed is None:
            return self
        if isinstance(allowed, list):
            if all(isinstance(item, str) and item for item in allowed):
                return self
            raise PydanticCustomError(
                "invalid_value",
                "{field} is invalid",
                {"field": "allowed_tools"},
            )
        if isinstance(allowed, dict):
            names = allowed.get("tool_names")
            if (
                set(allowed) <= {"tool_names"}
                and isinstance(names, list)
                and all(isinstance(item, str) and item for item in names)
            ):
                return self
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "allowed_tools"},
            )
        raise PydanticCustomError(
            "invalid_value",
            "{field} is invalid",
            {"field": "allowed_tools"},
        )


MAX_ALLOWED_DOMAINS = 10
_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


class WebSearchFilters(StrictModel):
    allowed_domains: list[str] | None = None

    @model_validator(mode="after")
    def domains_valid(self) -> Self:
        domains = self.allowed_domains
        if domains is None:
            return self
        if len(domains) > MAX_ALLOWED_DOMAINS or not all(
            _DOMAIN.fullmatch(item) for item in domains
        ):
            raise PydanticCustomError(
                "invalid_value",
                "{field} is invalid",
                {"field": "allowed_domains"},
            )
        return self


class WebSearchTool(StrictModel):
    type: Literal["web_search"]
    filters: WebSearchFilters | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in ("search_context_size", "user_location"):
                if field in data:
                    raise PydanticCustomError(
                        "not_implemented",
                        "{field} is not implemented",
                        {"field": field},
                    )
        return data


AgentTool = Annotated[
    FunctionTool | McpTool | WebSearchTool, Field(discriminator="type")
]


class SessionDefaults(StrictModel):
    environment: EnvironmentSpec | None = None
    vault_ids: list[uuid.UUID] | None = None


class Reasoning(StrictModel):
    effort: str | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def summary_not_implemented(self) -> Self:
        if self.summary is not None:
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "summary"},
            )
        return self


class AgentWrite(StrictModel):
    name: str | None = None
    model: str | None = None
    instructions: str | None = None
    idle_ttl: str | None = None
    metadata: dict[str, Any] | None = None
    tools: list[AgentTool] | None = None
    session_defaults: SessionDefaults | None = None
    reasoning: Reasoning | None = None
    service_tier: str | None = None
    text: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented(cls, data: Any) -> Any:
        if isinstance(data, dict):
            tools = data.get("tools")
            for tool in tools if isinstance(tools, list) else []:
                kind = tool.get("type") if isinstance(tool, dict) else None
                if isinstance(kind, str) and kind.startswith("web_search_preview"):
                    raise PydanticCustomError(
                        "not_implemented",
                        "{field} is not implemented",
                        {"field": "web_search_preview"},
                    )
            for field in (*_UNIMPLEMENTED, "service_tier", "text"):
                if field not in data:
                    continue
                if field == "service_tier" and data[field] in (None, "auto"):
                    continue
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
        "metadata": public_metadata(
            mirror_sandbox_metadata(agent.metadata_json, defaults)
        ),
        "tools": agent.tools,
        "session_defaults": defaults,
        "reasoning": reasoning_body(agent.metadata_json),
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


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


async def definition_for_session(
    db: Any, tenant_id: uuid.UUID, row: Any
) -> dict[str, Any] | None:
    if row.agent_id is None:
        return None
    agent = await get_agent(db, tenant_id, row.agent_id)
    if agent is None:
        return None
    return snapshot_from_agent(agent)


def reasoning_write(body: AgentWrite) -> tuple[object, bool] | None:
    if "reasoning" not in body.model_fields_set or body.reasoning is None:
        return None
    if "effort" not in body.reasoning.model_fields_set:
        return None
    reset = body.reasoning.effort is None
    return body.reasoning.effort, reset


def fold_reasoning(
    payload: dict[str, Any],
    body: AgentWrite,
    existing_metadata: dict[str, Any] | None,
) -> None:
    change = reasoning_write(body)
    if change is None:
        payload.pop("reasoning", None)
        return
    effort, reset = change
    if "metadata" in payload:
        reject_reasoning_conflict(
            payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else None,
            effort,
        )
        base = payload.get("metadata")
    else:
        base = existing_metadata
    payload["metadata"] = apply_reasoning_effort(
        base if isinstance(base, dict) else None, effort, reset=reset
    )
    payload.pop("reasoning", None)


def write_payload(body: AgentWrite) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    payload.pop("reasoning", None)
    payload.pop("service_tier", None)
    if "tools" in payload and body.tools is not None:
        payload["tools"] = [tool.model_dump(exclude_none=True) for tool in body.tools]
    if "session_defaults" in payload and body.session_defaults is not None:
        payload["session_defaults"] = body.session_defaults.model_dump(
            mode="json", exclude_none=True
        )
    return payload


class AgentService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        search: SearchResolver | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.search = search if search is not None else SearchResolver(settings)

    async def create(
        self,
        tenant_id: uuid.UUID,
        body: AgentWrite,
        *,
        api_key: str | None = None,
        check_model: bool = True,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        payload = write_payload(body)
        incoming_meta = payload.get("metadata")
        if isinstance(incoming_meta, dict):
            reject_removed_size_key(incoming_meta)
            reject_client_thinking_key(incoming_meta)
        fold_reasoning(payload, body, None)
        if "idle_ttl" in payload:
            payload["idle_ttl"] = normalize_idle_ttl(payload.get("idle_ttl"))
        self._normalize_defaults(
            payload, existing_metadata=None, existing_defaults=None
        )
        validate_pi_metadata(payload.get("metadata"))
        require_thinking_supported(
            self.settings,
            payload.get("model") if isinstance(payload.get("model"), str) else None,
            thinking_from_metadata(payload.get("metadata")),
        )
        validate_idle_metadata(payload.get("metadata"))
        validate_sandbox_metadata(self.settings, payload.get("metadata"))
        validate_defaults_shape(self.settings, payload.get("session_defaults"))
        reject_codemode_without_builtin_tools(payload.get("metadata"), None)
        if is_env_none(_defaults_environment(payload.get("session_defaults"))):
            reject_tools_for_env_none(payload.get("tools"))
            reject_builtin_tools_for_env_none(payload.get("metadata"), None)
        await require_search(
            self.search,
            payload.get("tools"),
            tenant_id=tenant_id,
            user_id=user_id,
            org_id=org_id,
        )
        if check_model:
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
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        payload = write_payload(body)
        incoming_meta = payload.get("metadata")
        if isinstance(incoming_meta, dict):
            reject_removed_size_key(incoming_meta)
            reject_client_thinking_key(incoming_meta)
        if "model" in payload:
            await require_saved_model(self.settings, payload.get("model"), api_key)
        async with self.store.session() as db:
            existing = await get_agent(db, tenant_id, agent_id)
            if existing is None:
                not_found()
            fold_reasoning(
                payload,
                body,
                existing.metadata_json
                if isinstance(existing.metadata_json, dict)
                else None,
            )
            if "idle_ttl" in payload:
                payload["idle_ttl"] = normalize_idle_ttl(payload.get("idle_ttl"))
            if (
                "metadata" in payload
                and isinstance(payload["metadata"], dict)
                and THINKING_KEY not in payload["metadata"]
                and isinstance(existing.metadata_json, dict)
                and THINKING_KEY in existing.metadata_json
            ):
                payload["metadata"] = {
                    **payload["metadata"],
                    THINKING_KEY: existing.metadata_json[THINKING_KEY],
                }
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
            stored = strip_removed_size_key(stored)
            validate_pi_metadata(stored)
            model = payload.get("model", existing.model)
            require_thinking_supported(
                self.settings,
                model if isinstance(model, str) else None,
                thinking_from_metadata(stored),
            )
            validate_idle_metadata(stored)
            validate_sandbox_metadata(self.settings, stored)
            if "session_defaults" in payload and isinstance(
                payload.get("session_defaults"), dict
            ):
                validate_defaults_shape(self.settings, payload["session_defaults"])
                await require_default_refs(db, tenant_id, payload["session_defaults"])
            tools = payload.get("tools", existing.tools)
            if "session_defaults" in payload:
                effective_defaults = payload["session_defaults"]
            else:
                effective_defaults = (
                    existing.session_defaults
                    if isinstance(existing.session_defaults, dict)
                    else None
                )
            env = _defaults_environment(
                effective_defaults if isinstance(effective_defaults, dict) else None
            )
            if "metadata" in payload:
                reject_codemode_without_builtin_tools(payload.get("metadata"), None)
            else:
                reject_codemode_without_builtin_tools(stored, None)
            if is_env_none(env):
                reject_tools_for_env_none(tools if isinstance(tools, list) else None)
                reject_builtin_tools_for_env_none(stored, None)
            if "tools" in payload:
                await require_search(
                    self.search,
                    payload["tools"],
                    tenant_id=tenant_id,
                    user_id=user_id,
                    org_id=org_id,
                )
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
            meta = strip_removed_size_key(meta)
            payload["metadata"] = meta
        if defaults is not None:
            payload["session_defaults"] = defaults

    async def delete(self, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            deleted = await delete_agent(db, tenant_id, agent_id)
            if not deleted:
                not_found()
        return {"id": str(agent_id), "deleted": True}
