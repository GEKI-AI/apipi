import contextlib
import contextvars
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterator
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
_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "apipi_log_context", default=None
)
WARNING_SUMMARY_SECONDS = 60.0


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


def bind_log_context(**values: object) -> contextvars.Token[dict[str, Any] | None]:
    return _context.set({**(_context.get() or {}), **log_fields(**values)})


def unbind_log_context(token: contextvars.Token[dict[str, Any] | None]) -> None:
    _context.reset(token)


@contextlib.contextmanager
def log_context(**values: object) -> Iterator[None]:
    """Add fields to every log line written in this task and the tasks it starts.

    Used for `worker_id` and `connection_id`, so the lines of one socket
    join across the API and the worker. A field a line sets itself wins.
    """
    token = bind_log_context(**values)
    try:
        yield
    finally:
        unbind_log_context(token)


def current_log_context() -> dict[str, Any]:
    return dict(_context.get() or {})


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            if key not in record.__dict__:
                setattr(record, key, value)
        return True


class RateLimitedLog:
    """Warnings that repeat are logged once, then summarized.

    The first warning of a `key` is written at once with `count` 1. The
    next ones are only counted. When `interval` seconds have passed since
    the last written line, the next warning is written with `count` set
    to the number of occurrences since then. Keep one per connection or
    per process, and use the `event` name as the key.
    """

    def __init__(
        self,
        log: logging.Logger,
        *,
        interval: float = WARNING_SUMMARY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self._interval = interval
        self._clock = clock
        self._state: dict[str, tuple[float, int]] = {}

    def warning(
        self,
        message: str,
        *,
        event: str,
        error_code: str | None = None,
        key: str | None = None,
        exc_info: bool | BaseException = False,
        **values: object,
    ) -> bool:
        name = key if key is not None else event
        now = self._clock()
        last, pending = self._state.get(name, (None, 0))
        pending += 1
        if last is not None and now - last < self._interval:
            self._state[name] = (last, pending)
            return False
        self._state[name] = (now, 0)
        log_event(
            self._log,
            logging.WARNING,
            message,
            event=event,
            error_code=error_code,
            exc_info=exc_info,
            count=pending,
            **values,
        )
        return True


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
        _handler.addFilter(ContextFilter())
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
        "filters": {"context": {"()": "apipi.common.logutil.ContextFilter"}},
        "formatters": {"default": formatter},
        "handlers": {
            "default": {
                "class": "apipi.common.logutil.FlushStreamHandler",
                "filters": ["context"],
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
