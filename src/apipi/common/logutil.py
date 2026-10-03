import contextlib
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

SERVICE = "apipi"
TEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"
_SECRET_KEY = re.compile(
    r"(authorization|bearer|api[_-]?key|token|password|secret|cookie|master[_-]?key)",
    re.I,
)
_SKIP_RECORD = frozenset(logging.makeLogRecord({}).__dict__) | {"message"}
_handler: logging.Handler | None = None


def log_fields(**values: object) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            extra[key] = value
            continue
        if isinstance(value, UUID):
            extra[key] = str(value)
            continue
        if isinstance(value, str):
            if value:
                extra[key] = value
            continue
        extra[key] = value
    return extra


def log_event(
    log: logging.Logger,
    level: int,
    message: str,
    *,
    event: str,
    error_code: str | None = None,
    exc_info: bool | BaseException = False,
    **values: object,
) -> None:
    extra = log_fields(event=event, error_code=error_code, **values)
    log.log(level, message, extra=extra, exc_info=exc_info)


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
        with contextlib.suppress(OSError, ValueError):
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
        formatter = {"()": "apipi.common.logutil.JsonFormatter"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "default": {
                "class": "apipi.common.logutil.FlushStreamHandler",
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
