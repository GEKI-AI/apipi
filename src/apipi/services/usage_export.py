import asyncio
import importlib
import logging
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from apipi.config import ConfigError, Settings
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics

log = logging.getLogger("apipi")

_sink_cache: dict[str, "EventSink"] = {}


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


def split_sink_paths(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_sink(path: str, setting: str) -> EventSink:
    cached = _sink_cache.get(path)
    if cached is not None:
        return cached
    if ":" not in path:
        raise ConfigError(f"{setting} must be package.mod:Class")
    module_name, attr_name = path.rsplit(":", 1)
    if not module_name or not attr_name:
        raise ConfigError(f"{setting} must be package.mod:Class")
    try:
        module = importlib.import_module(module_name)
        attr = getattr(module, attr_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"{setting} sink not found: {path}") from exc
    sink = attr() if isinstance(attr, type) or callable(attr) else attr
    if not hasattr(sink, "emit"):
        raise ConfigError(f"{setting} sink missing emit: {path}")
    _sink_cache[path] = sink
    return sink


def load_custom_sinks(paths: str, setting: str) -> list[EventSink]:
    return [load_sink(path, setting) for path in split_sink_paths(paths)]


def emit_all(
    sinks: list[EventSink],
    event: dict[str, Any],
    *,
    failed: str,
    drop_event: str,
) -> None:
    for sink in sinks:
        try:
            sink.emit(event)
        except Exception:
            log_event(
                log,
                logging.WARNING,
                failed,
                event=drop_event,
                error_code="export_drop",
                exc_info=True,
                tenant_id=event.get("tenant_id"),
                session_id=event.get("session_id"),
                turn_id=event.get("turn_id"),
                request_id=event.get("request_id"),
            )


class HttpExporter:
    def __init__(
        self,
        *,
        url: str | None,
        token: str | None,
        timeout: float,
        retries: int,
        observe: Callable[[str], None] | None = None,
        dropped: str = "export dropped",
        drop_event: str = "usage.export.dropped",
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._retries = retries
        self._observe = observe
        self._dropped = dropped
        self._drop_event = drop_event
        self._tasks: set[asyncio.Task[None]] = set()

    def emit(self, event: dict[str, Any]) -> None:
        if not self._url:
            return
        try:
            task = asyncio.create_task(self._post(event))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, event: dict[str, Any]) -> None:
        url = self._url
        if url is None:
            return
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        attempts = self._retries + 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=event, headers=headers)
                if response.status_code < 500:
                    self._result("ok" if response.is_success else "drop")
                    return
                last = httpx.HTTPStatusError(
                    f"{response.status_code}",
                    request=response.request,
                    response=response,
                )
            except Exception as exc:
                last = exc
        self._result("drop")
        if last is not None:
            log_event(
                log,
                logging.WARNING,
                self._dropped,
                event=self._drop_event,
                error_code="export_drop",
                tenant_id=event.get("tenant_id"),
                session_id=event.get("session_id"),
                turn_id=event.get("turn_id"),
                request_id=event.get("request_id"),
            )

    def _result(self, result: str) -> None:
        if self._observe is not None:
            self._observe(result)


class UsageExporter:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        observe = None
        if isinstance(metrics, Metrics):
            observe = metrics.observe_usage_export
        self._http = HttpExporter(
            url=settings.usage_export_url,
            token=settings.usage_export_token,
            timeout=settings.usage_export_timeout.total_seconds(),
            retries=settings.usage_export_retries,
            observe=observe,
            dropped="usage export dropped",
            drop_event="usage.export.dropped",
        )

    def emit(self, event: dict[str, Any]) -> None:
        self._http.emit(event)

    async def _post(self, event: dict[str, Any]) -> None:
        await self._http._post(event)


def load_usage_sinks(
    settings: Settings, metrics: Metrics | None = None
) -> list[EventSink]:
    sinks: list[EventSink] = []
    if settings.usage_export_url:
        sinks.append(UsageExporter(settings, metrics))
    sinks.extend(load_custom_sinks(settings.usage_sinks, "APIPI_USAGE_SINKS"))
    return sinks


def export_usage(
    settings: Settings | None, metrics: Metrics | None, event: dict[str, Any]
) -> None:
    if settings is None:
        return
    emit_all(
        load_usage_sinks(settings, metrics),
        event,
        failed="usage sink failed",
        drop_event="usage.export.dropped",
    )
