from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.type, exc.message, exc.code),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        field = _not_implemented_field(exc)
        if field is not None:
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
            return JSONResponse(
                status_code=400,
                content=error_body(
                    "invalid_request",
                    f"Unknown field: {field}",
                    "unknown_field",
                ),
            )
        return JSONResponse(
            status_code=400,
            content=error_body(
                "invalid_request", "Invalid request", "validation_error"
            ),
        )
