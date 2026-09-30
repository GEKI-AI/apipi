import uuid
from typing import Any

from fastapi import Request

from apipi.gateway.auth import check_authorize, not_found
from apipi.store.repo import (
    get_agent,
    get_session,
)


async def agent_exists(
    request: Request, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    async with request.app.state.store.session() as db:
        return await get_agent(db, tenant_id, agent_id) is not None


async def session_agent_id(
    request: Request,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: str | None = None,
) -> uuid.UUID | None:
    async with request.app.state.store.session() as db:
        row = await get_session(db, tenant_id, session_id, user_id=user_id)
        if row is None:
            return None
        return row.agent_id


async def require_session_agent(
    request: Request,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    action: str,
    user_id: str | None = None,
) -> uuid.UUID | None:
    async with request.app.state.store.session() as db:
        row = await get_session(db, tenant_id, session_id, user_id=user_id)
        if row is None:
            not_found()
        agent_id = row.agent_id
    await check_authorize(
        request,
        action=action,
        resource_type="agent",
        resource_id=str(agent_id) if agent_id is not None else None,
    )
    return agent_id


async def require_agent_route(
    request: Request,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    action: str,
) -> None:
    async with request.app.state.store.session() as db:
        if await get_agent(db, tenant_id, agent_id) is None:
            not_found()
    await check_authorize(
        request, action=action, resource_type="agent", resource_id=str(agent_id)
    )


async def require_id_route(
    request: Request,
    tenant_id: uuid.UUID,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    exists: Any,
) -> None:
    found = await exists
    if not found:
        not_found()
    await check_authorize(
        request, action=action, resource_type=resource_type, resource_id=resource_id
    )


def filter_ids(
    items: list[dict[str, Any]], key: str, allowed: frozenset[str] | None
) -> list[dict[str, Any]]:
    if allowed is None:
        return items
    return [a for a in items if str(a.get(key)) in allowed]
