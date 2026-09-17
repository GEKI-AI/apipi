from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.api.deps import model_key
from apipi.gateway.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


@router.get("/v1/models")
async def list_models(
    request: Request,
    _tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    return request.app.state.gateway.models.list(model_key(request))
