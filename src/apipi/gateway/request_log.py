import logging
import time
from typing import Any
from uuid import UUID

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.gateway.http_path import skip_request_path
from apipi.gateway.metrics import route_path

_SKIP = frozenset({"/health", "/metrics"})
_http = logging.getLogger("apipi.http")


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
            path = scope.get("path")
            route = path if isinstance(path, str) and path else route_path(scope)
            _http.info(
                "request start",
                extra={"method": method, "route": route},
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
            error_code = _state_text(state, "error_code")
            if error_code is not None:
                extra["error_code"] = error_code
            app = scope.get("app")
            settings = getattr(getattr(app, "state", None), "settings", None)
            instance_id = getattr(settings, "instance_id", None)
            if isinstance(instance_id, str) and instance_id:
                extra["instance_id"] = instance_id
            _http.info("request", extra=extra)
