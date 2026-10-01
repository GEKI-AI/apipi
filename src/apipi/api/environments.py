import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.gateway.auth import not_found, require_tenant
from apipi.services.runtime import EventHub
from apipi.services.sandbox_status import environment_public, expire_if_stale
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import get_session, get_tenant_environment

router = APIRouter()


@router.get("/v1/agents/environments/{environment_id}")
async def read_environment(
    environment_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    store: Store = request.app.state.store
    hub: EventHub = request.app.state.event_hub
    async with store.session() as db:
        env = await get_tenant_environment(db, tenant.id, environment_id)
        if env is None:
            not_found()
        row = await get_session(db, tenant.id, env.session_id)
        if row is None:
            not_found()
        await expire_if_stale(db, hub, tenant.id, row)
        return environment_public(row, env.status)
