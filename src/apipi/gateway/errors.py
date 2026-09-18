import logging
from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from apipi.gateway.logutil import log_event

log = logging.getLogger("apipi")


class ApiError(Exception):
    def __init__(
        self,
        type: str,
        message: str,
        *,
        code: str = "",
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.code = code
        self.status_code = status_code


def error_body(type: str, message: str, code: str = "") -> dict[str, Any]:
    return {"error": {"type": type, "code": code, "message": message}}


def not_implemented(code: str, message: str | None = None) -> NoReturn:
    raise ApiError(
        "not_implemented",
        message if message is not None else f"{code} is not implemented",
        code=code,
        status_code=400,
    )


def gone(message: str = "Gone") -> NoReturn:
    raise ApiError("invalid_request", message, code="gone", status_code=410)


def _unknown_field(exc: RequestValidationError | ValidationError) -> str | None:
    for error in exc.errors():
        if error.get("type") == "extra_forbidden":
            loc = error.get("loc", ())
            field = loc[-1] if loc else "unknown"
            return str(field)
    return None


def _not_implemented_field(exc: RequestValidationError | ValidationError) -> str | None:
    for error in exc.errors():
        if error.get("type") == "not_implemented":
            ctx = error.get("ctx") or {}
            field = ctx.get("field")
            if field is None:
                loc = error.get("loc", ())
                field = loc[-1] if loc else "unknown"
            return str(field)
    return None


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _tenant_id(request: Request) -> object:
    return getattr(request.state, "tenant_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        request.state.error_code = exc.code or exc.type
        if exc.status_code >= 500:
            log_event(
                log,
                logging.ERROR,
                "api error",
                event="api.error",
                error_code=exc.code or exc.type,
                request_id=_request_id(request),
                tenant_id=_tenant_id(request),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.type, exc.message, exc.code),
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        request.state.error_code = "internal"
        log_event(
            log,
            logging.ERROR,
            "api error",
            event="api.error",
            error_code="internal",
            exc_info=exc,
            request_id=_request_id(request),
            tenant_id=_tenant_id(request),
        )
        return JSONResponse(
            status_code=500,
            content=error_body("api_error", "Internal error", "internal"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        field = _not_implemented_field(exc)
        if field is not None:
            request.state.error_code = field
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "not_implemented",
                    f"{field} is not implemented",
                    field,
                ),
            )
        field = _unknown_field(exc)
        if field is not None:
            request.state.error_code = "unknown_field"
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_request",
                    f"Unknown field: {field}",
                    "unknown_field",
                ),
            )
        request.state.error_code = "validation_error"
        return JSONResponse(
            status_code=400,
            content=error_body(
                "invalid_request", "Invalid request", "validation_error"
            ),
        )
