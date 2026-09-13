import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

from apipi.config import Settings
from apipi.metrics import Metrics

log = logging.getLogger("apipi")


class HttpExporter:
    def __init__(
        self,
        *,
        url: str | None,
        token: str | None,
        timeout: float,
        retries: int,
        observe: Callable[[str], None] | None = None,
        dropped: str = "export dropped: %s",
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._retries = retries
        self._observe = observe
        self._dropped = dropped
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
            log.warning(self._dropped, last)

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
            dropped="usage export dropped: %s",
        )

    def emit(self, event: dict[str, Any]) -> None:
        self._http.emit(event)

    async def _post(self, event: dict[str, Any]) -> None:
        await self._http._post(event)


def export_usage(
    settings: Settings | None, metrics: Metrics | None, event: dict[str, Any]
) -> None:
    if settings is None or not settings.usage_export_url:
        return
    UsageExporter(settings, metrics).emit(event)
