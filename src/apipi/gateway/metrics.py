from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.common.metrics import Metrics, tenant_label
from apipi.gateway.http_path import skip_request_path

_SKIP = frozenset({"/health", "/metrics"})


def route_path(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or skip_request_path(scope, _SKIP):
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        metrics = getattr(getattr(app, "state", None), "metrics", None)
        if not isinstance(metrics, Metrics):
            await self.app(scope, receive, send)
            return
        status_box = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_box["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            state = scope.get("state")
            tenant_id = state.get("tenant_id") if isinstance(state, dict) else None
            error_code = state.get("error_code") if isinstance(state, dict) else None
            metrics.observe_request(
                tenant=tenant_label(
                    tenant_id if isinstance(tenant_id, UUID | str) else None
                ),
                method=str(scope.get("method", "")),
                path=route_path(scope),
                status=status_box["status"],
                error_code=error_code if isinstance(error_code, str) else None,
            )


def mount_metrics(app: FastAPI, metrics: Metrics) -> None:
    app.add_middleware(MetricsMiddleware)

    @app.get("/metrics")
    def scrape() -> Response:
        return Response(content=metrics.scrape(), media_type=CONTENT_TYPE_LATEST)
