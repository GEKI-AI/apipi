from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.api.deps import model_key
from apipi.gateway.auth import require_tenant
from apipi.store.models import Tenant

router = APIRouter()


@router.get("/v1/apipi/models")
async def list_model_capabilities(
    request: Request,
    _tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    registry = request.app.state.settings.model_registry
    data = []
    for model_id, raw in registry.items():
        item = dict(raw) if isinstance(raw, dict) else {}
        item["id"] = model_id
        data.append(item)
    return {"object": "list", "data": data}


@router.get("/v1/models")
async def list_models(
    request: Request,
    _tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    return await request.app.state.gateway.models.list(model_key(request))
