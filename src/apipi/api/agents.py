import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.auth import get_db, not_found, require_tenant
from apipi.store.models import Agent, Tenant
from apipi.store.repo import get_agent

router = APIRouter()


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
