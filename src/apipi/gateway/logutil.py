import contextlib
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.gateway.http_path import skip_request_path
from apipi.gateway.metrics import route_path

SERVICE = "apipi"
TEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"
_SKIP = frozenset({"/health", "/metrics"})
_SECRET_KEY = re.compile(
    r"(authorization|bearer|api[_-]?key|token|password|secret|cookie)",
    re.I,
)
_SKIP_RECORD = frozenset(logging.makeLogRecord({}).__dict__) | {"message"}
_handler: logging.Handler | None = None
_http = logging.getLogger("apipi.http")


def redact_value(key: str, value: object) -> object:
    if _SECRET_KEY.search(key):
        return "[redacted]"
    return value


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), _jsonable(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return str(value)


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _SKIP_RECORD or key.startswith("_") or value is None:
            continue
        fields[key] = redact_value(key, _jsonable(value))
    return fields


class FlushStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE,
        }
        payload.update(extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, level: str = "info", format: str = "json") -> None:
    global _handler
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure):
        with contextlib.suppress(OSError):
            reconfigure(line_buffering=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    formatter: logging.Formatter = (
        logging.Formatter(TEXT_FORMAT) if format == "text" else JsonFormatter()
    )
    if _handler is None:
        _handler = FlushStreamHandler(sys.stderr)
        root.addHandler(_handler)
    _handler.setFormatter(formatter)
    _handler.setLevel(level.upper())


def uvicorn_log_config(*, level: str, format: str) -> dict[str, Any]:
    formatter: dict[str, Any]
    if format == "text":
        formatter = {"format": TEXT_FORMAT}
    else:
        formatter = {"()": "apipi.gateway.logutil.JsonFormatter"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "default": {
                "class": "apipi.gateway.logutil.FlushStreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            }
        },
        "root": {"handlers": ["default"], "level": level.upper()},
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": level.upper(),
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": level.upper(),
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": level.upper(),
                "propagate": False,
            },
        },
    }


def _state_text(state: object, key: str) -> str | None:
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    text = str(value).strip()
    return text or None


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or skip_request_path(scope, _SKIP):
            await self.app(scope, receive, send)
            return
        status_box = {"status": 500}
        started = time.perf_counter()
        method = str(scope.get("method", ""))
        if method in {"POST", "PUT", "PATCH"}:
            _http.info(
                "request start",
                extra={"method": method, "route": route_path(scope)},
            )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            state = scope.get("state")
            extra: dict[str, Any] = {
                "method": str(scope.get("method", "")),
                "route": route_path(scope),
                "status": status_box["status"],
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
            request_id = _state_text(state, "request_id")
            if request_id is not None:
                extra["request_id"] = request_id
            tenant_id = _state_text(state, "tenant_id")
            if tenant_id is not None:
                extra["tenant_id"] = tenant_id
            app = scope.get("app")
            settings = getattr(getattr(app, "state", None), "settings", None)
            instance_id = getattr(settings, "instance_id", None)
            if isinstance(instance_id, str) and instance_id:
                extra["instance_id"] = instance_id
            _http.info("request", extra=extra)
