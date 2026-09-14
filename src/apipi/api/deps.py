from fastapi import Request

from apipi.errors import ApiError


def model_key(request: Request) -> str:
    overwrite = request.app.state.settings.model_api_key_overwrite
    if isinstance(overwrite, str) and overwrite:
        return overwrite
    bearer = getattr(request.state, "bearer", None)
    if isinstance(bearer, str) and bearer:
        return bearer
    raise ApiError(
        "invalid_request",
        "Invalid bearer token",
        code="unauthorized",
        status_code=401,
    )
