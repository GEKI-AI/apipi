import uuid
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.auth import get_db, not_found, require_tenant
from apipi.schemas import StrictModel
from apipi.store.models import Agent, Tenant
from apipi.store.repo import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    update_agent,
)

router = APIRouter()

_UNIMPLEMENTED = ("multi_agent", "tool_search", "programmatic_tool_calling")


class FunctionTool(StrictModel):
    type: Literal["function"]
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class McpTool(StrictModel):
    type: Literal["mcp"]
    server_label: str
    server_url: str | None = None
    headers: dict[str, str] | None = None
    command: str | None = None
    args: list[str] | None = None

    @model_validator(mode="after")
    def http_or_stdio(self) -> Self:
        http = self.server_url is not None
        stdio = self.command is not None
        if http == stdio:
            raise ValueError("mcp tool needs server_url or command, not both")
        return self


AgentTool = Annotated[FunctionTool | McpTool, Field(discriminator="type")]


class AgentWrite(StrictModel):
    name: str | None = None
    model: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] | None = None
    tools: list[AgentTool] | None = None

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
    return {
        "id": str(agent.id),
        "name": agent.name,
        "model": agent.model,
        "instructions": agent.instructions,
        "metadata": agent.metadata_json,
        "tools": agent.tools,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


def _tools_payload(
    tools: list[FunctionTool | McpTool] | None,
) -> list[dict[str, Any]] | None:
    if tools is None:
        return None
    return [tool.model_dump(exclude_none=True) for tool in tools]


def _write_payload(body: AgentWrite) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    if "tools" in payload:
        payload["tools"] = _tools_payload(body.tools)
    return payload


@router.post("/v1/agents")
async def create_saved_agent(
    body: AgentWrite,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    payload = _write_payload(body)
    agent = await create_agent(
        db,
        tenant.id,
        name=payload.get("name"),
        model=payload.get("model"),
        instructions=payload.get("instructions"),
        metadata=payload.get("metadata"),
        tools=payload.get("tools"),
    )
    return agent_body(agent)


@router.get("/v1/agents")
async def list_saved_agents(
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    agents = await list_agents(db, tenant.id)
    return {"data": [agent_body(agent) for agent in agents]}


@router.get("/v1/agents/{agent_id}")
async def read_agent(
    agent_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    agent = await get_agent(db, tenant.id, agent_id)
    if agent is None:
        not_found()
    return agent_body(agent)


@router.post("/v1/agents/{agent_id}")
async def update_saved_agent(
    agent_id: uuid.UUID,
    body: AgentWrite,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    agent = await update_agent(db, tenant.id, agent_id, changes=_write_payload(body))
    if agent is None:
        not_found()
    return agent_body(agent)


@router.delete("/v1/agents/{agent_id}")
async def delete_saved_agent(
    agent_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    deleted = await delete_agent(db, tenant.id, agent_id)
    if not deleted:
        not_found()
    return {"id": str(agent_id), "deleted": True}
