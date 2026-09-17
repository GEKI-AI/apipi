from typing import Any

from apipi.config import Settings
from apipi.gateway.errors import ApiError, not_implemented
from apipi.worker.pi.model_host import fetch_models_json


class ModelsService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list(self, api_key: str | None) -> Any:
        if not self.settings.forward_models:
            not_implemented("forward_models", "GET /v1/models is disabled")
        base = self.settings.model_base_url
        if not isinstance(base, str) or not base:
            raise ApiError(
                "invalid_request",
                "Model host /models is unreachable",
                code="model_host_unreachable",
                status_code=400,
            )
        return fetch_models_json(base, api_key)
