import re
import uuid
from typing import Any

from apipi.config import Settings
from apipi.metrics import Metrics
from apipi.store.models import Item
from apipi.usage_export import HttpExporter

_BEARER = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")


def _secrets(settings: Settings) -> tuple[str, ...]:
    values = (
        settings.model_api_key,
        settings.usage_export_token,
        settings.payload_export_token,
    )
    return tuple(value for value in values if isinstance(value, str) and value)


def redact_payload(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        text = value
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        return _BEARER.sub(r"\1[redacted]", text)
    if isinstance(value, dict):
        return {key: redact_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, secrets) for item in value]
    return value


def payload_event(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    request_id: str | None,
    items: list[Item],
) -> dict[str, Any]:
    exported: list[dict[str, Any]] = []
    for item in items:
        if item.turn_id != turn_id:
            continue
        entry: dict[str, Any] = {"type": item.type}
        data = item.data if isinstance(item.data, dict) else {}
        for key, value in data.items():
            if key != "type":
                entry[key] = value
        exported.append(entry)
    return {
        "tenant_id": str(tenant_id),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "request_id": request_id,
        "items": exported,
    }


class PayloadExporter:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        observe = None
        if isinstance(metrics, Metrics):
            observe = metrics.observe_payload_export
        self._http = HttpExporter(
            url=settings.payload_export_url,
            token=settings.payload_export_token,
            timeout=settings.payload_export_timeout.total_seconds(),
            retries=settings.payload_export_retries,
            observe=observe,
            dropped="payload export dropped: %s",
        )

    def emit(self, event: dict[str, Any]) -> None:
        self._http.emit(event)

    async def _post(self, event: dict[str, Any]) -> None:
        await self._http._post(event)


def export_payload(
    settings: Settings | None,
    metrics: Metrics | None,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    request_id: str | None,
    items: list[Item],
) -> None:
    if settings is None or not settings.payload_export_url:
        return
    event = payload_event(
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        request_id=request_id,
        items=items,
    )
    PayloadExporter(settings, metrics).emit(redact_payload(event, _secrets(settings)))
