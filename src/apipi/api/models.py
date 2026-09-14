from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apipi.api.deps import model_key
from apipi.auth import require_tenant
from apipi.errors import ApiError, not_implemented
from apipi.pi.model_host import fetch_models_json
from apipi.store.models import Tenant

router = APIRouter()


@router.get("/v1/models")
async def list_models(
    request: Request,
    _tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    settings = request.app.state.settings
    if not settings.forward_models:
        not_implemented("forward_models", "GET /v1/models is disabled")
    base = settings.model_base_url
    if not isinstance(base, str) or not base:
        raise ApiError(
            "invalid_request",
            "Model host /models is unreachable",
            code="model_host_unreachable",
            status_code=400,
        )
    return fetch_models_json(base, model_key(request))
