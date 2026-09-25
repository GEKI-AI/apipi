import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.gateway.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


def _usage(request: Request) -> Any:
    return request.app.state.gateway.usage


@router.get("/v1/usage")
async def get_usage(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    session_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    day: date | None = None,
) -> dict[str, int]:
    user = getattr(request.state, "user_id", None)
    user_id = user if isinstance(user, str) and user else None
    return await _usage(request).get(
        tenant.id,
        session_id=session_id,
        turn_id=turn_id,
        day=day,
        user_id=user_id,
    )
