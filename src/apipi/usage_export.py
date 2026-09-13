import asyncio
import logging
from typing import Any

import httpx

from apipi.config import Settings
from apipi.metrics import Metrics

log = logging.getLogger("apipi")


class UsageExporter:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        self._url = settings.usage_export_url
        self._token = settings.usage_export_token
        self._timeout = settings.usage_export_timeout.total_seconds()
        self._retries = settings.usage_export_retries
        self._metrics = metrics
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
                    self._observe("ok" if response.is_success else "drop")
                    return
                last = httpx.HTTPStatusError(
                    f"{response.status_code}",
                    request=response.request,
                    response=response,
                )
            except Exception as exc:
                last = exc
        self._observe("drop")
        if last is not None:
            log.warning("usage export dropped: %s", last)

    def _observe(self, result: str) -> None:
        if isinstance(self._metrics, Metrics):
            self._metrics.observe_usage_export(result)


def export_usage(
    settings: Settings | None, metrics: Metrics | None, event: dict[str, Any]
) -> None:
    if settings is None or not settings.usage_export_url:
        return
    UsageExporter(settings, metrics).emit(event)
