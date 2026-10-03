import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.api.deps import model_key
from apipi.gateway.auth import check_authorize, require_tenant
from apipi.services.agents import AgentWrite
from apipi.store.models import Tenant
from apipi.store.repo import get_agent

router = APIRouter()


def _agents(request: Request) -> Any:
    return request.app.state.gateway.agents


def _state_str(request: Request, name: str) -> str | None:
    value = getattr(request.state, name, None)
    return value if isinstance(value, str) and value else None


async def _existing_agent_id(
    request: Request, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    store = request.app.state.store
    async with store.session() as db:
        return await get_agent(db, tenant_id, agent_id) is not None


def _apply_agent_filter(
    payload: dict[str, Any], allowed: frozenset[str] | None
) -> dict[str, Any]:
    if allowed is None:
        return payload
    items = payload.get("data", [])
    kept = [a for a in items if str(a.get("id")) in allowed]
    out = dict(payload)
    out["data"] = kept
    return out


@router.post("/v1/agents")
async def create_saved_agent(
    body: AgentWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await check_authorize(
        request, action="agent.write", resource_type="agent", resource_id=None
    )
    return await _agents(request).create(
        tenant.id,
        body,
        api_key=await model_key(request),
        user_id=_state_str(request, "user_id"),
        org_id=_state_str(request, "org_id"),
    )


@router.get("/v1/agents")
async def list_saved_agents(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    filt = await check_authorize(
        request, action="agent.list", resource_type="agent", resource_id=None
    )
    payload = await _agents(request).list(tenant.id)
    if filt is not None:
        payload = _apply_agent_filter(payload, filt.ids)
    return payload


@router.get("/v1/agents/{agent_id}")
async def read_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    if not await _existing_agent_id(request, tenant.id, agent_id):
        from apipi.gateway.auth import not_found

        not_found()
    await check_authorize(
        request,
        action="agent.read",
        resource_type="agent",
        resource_id=str(agent_id),
    )
    return await _agents(request).get(tenant.id, agent_id)


@router.post("/v1/agents/{agent_id}")
async def update_saved_agent(
    agent_id: uuid.UUID,
    body: AgentWrite,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    if not await _existing_agent_id(request, tenant.id, agent_id):
        from apipi.gateway.auth import not_found

        not_found()
    await check_authorize(
        request,
        action="agent.write",
        resource_type="agent",
        resource_id=str(agent_id),
    )
    return await _agents(request).update(
        tenant.id,
        agent_id,
        body,
        api_key=await model_key(request),
        user_id=_state_str(request, "user_id"),
        org_id=_state_str(request, "org_id"),
    )


@router.delete("/v1/agents/{agent_id}")
async def delete_saved_agent(
    agent_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    if not await _existing_agent_id(request, tenant.id, agent_id):
        from apipi.gateway.auth import not_found

        not_found()
    await check_authorize(
        request,
        action="agent.write",
        resource_type="agent",
        resource_id=str(agent_id),
    )
    return await _agents(request).delete(tenant.id, agent_id)
