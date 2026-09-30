from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.gateway.auth import check_authorize, require_tenant
from apipi.gateway.schemas import StrictModel
from apipi.store.models import Tenant

router = APIRouter()


class InvalidateBody(StrictModel):
    key_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None


@router.post("/v1/apipi/auth/invalidate")
async def invalidate_auth(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    body: InvalidateBody | None = None,
) -> dict[str, Any]:
    await check_authorize(
        request,
        action="auth.invalidate",
        resource_type="auth",
        resource_id=None,
    )
    filt = body if body is not None else InvalidateBody()
    cache = request.app.state.auth_cache

    def _matches(identity: object) -> bool:
        from apipi.gateway.auth import AuthIdentity as _I

        if not isinstance(identity, _I):
            return False
        if identity.tenant_id != tenant.id:
            return False
        if filt.key_id is not None and identity.key_id != filt.key_id:
            return False
        if filt.user_id is not None and identity.user_id != filt.user_id:
            return False
        if filt.org_id is not None:
            return identity.org_id == filt.org_id
        return True

    count = cache.invalidate_where(_matches)
    return {"invalidated": count}
