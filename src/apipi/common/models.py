import httpx

from apipi.common.errors import ApiError
from apipi.config import ConfigError, Settings

_listed: list[str] | None = None


def clear_model_cache() -> None:
    global _listed
    _listed = None


def remember_models(ids: list[str]) -> None:
    global _listed
    _listed = list(ids)


def _headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _unreachable() -> ApiError:
    return ApiError(
        "invalid_request",
        "Model host /models is unreachable",
        code="model_host_unreachable",
        status_code=400,
    )


def _unauthorized() -> ApiError:
    return ApiError(
        "invalid_request",
        "Model host rejected the API key",
        code="model_host_unauthorized",
        status_code=401,
    )


def _status_error(status: int) -> ApiError | None:
    if status in {401, 403}:
        return _unauthorized()
    if status >= 400:
        return _unreachable()
    return None


def models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def parse_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return ids


def fetch_model_ids(base_url: str, api_key: str | None = None) -> list[str]:
    try:
        response = httpx.get(
            models_url(base_url), headers=_headers(api_key), timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable") from exc
    if response.status_code in {401, 403}:
        return []
    if response.status_code >= 400:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable") from exc
    return parse_model_ids(payload)


async def fetch_models_json(base_url: str, api_key: str | None = None) -> object:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(models_url(base_url), headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise _unreachable() from exc
    error = _status_error(response.status_code)
    if error is not None:
        raise error
    try:
        return response.json()
    except ValueError as exc:
        raise _unreachable() from exc


async def listed_models(base_url: str, api_key: str | None = None) -> list[str]:
    return parse_model_ids(await fetch_models_json(base_url, api_key))


async def require_saved_model(
    settings: Settings, model: str | None, api_key: str | None = None
) -> None:
    if not isinstance(model, str) or not model.strip():
        return
    name = model.strip()
    if settings.model_list == "off":
        if settings.models:
            require_listed_model(name, list(settings.models))
        return
    if settings.model_list == "probe" and _listed is not None:
        require_listed_model(name, _listed)
        return
    if not settings.model_base_url:
        return
    ids = await listed_models(
        settings.model_base_url, api_key or settings.model_api_key_overwrite
    )
    if settings.model_list == "probe":
        remember_models(ids)
    require_listed_model(name, ids)


def require_model(model: str | None) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ApiError(
            "invalid_request",
            "agent.model is required",
            code="model_required",
            status_code=400,
        )
    return model.strip()


def require_listed_model(model: str, ids: list[str]) -> None:
    if model not in ids:
        raise ApiError(
            "invalid_request",
            f"model {model} is not available on OPENAI_BASE_URL",
            code="model_not_found",
            status_code=400,
        )
