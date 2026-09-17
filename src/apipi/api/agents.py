import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.agents import AgentWrite
from apipi.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


def _agents(request: Request) -> Any:
    return request.app.state.gateway.agents


@router.post("/v1/agents")
async def create_saved_agent(
    body: AgentWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _agents(request).create(tenant.id, body)


@router.get("/v1/agents")
async def list_saved_agents(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _agents(request).list(tenant.id)


@router.get("/v1/agents/{agent_id}")
async def read_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _agents(request).get(tenant.id, agent_id)


@router.post("/v1/agents/{agent_id}")
async def update_saved_agent(
    agent_id: uuid.UUID,
    body: AgentWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _agents(request).update(tenant.id, agent_id, body)


@router.delete("/v1/agents/{agent_id}")
async def delete_saved_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return await _agents(request).delete(tenant.id, agent_id)
