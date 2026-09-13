from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

disable_created_metrics()

_SKIP = frozenset({"/health", "/metrics"})
_LATENCY_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)
_TOKEN_KINDS = (
    ("prompt_tokens", "prompt"),
    ("completion_tokens", "completion"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
    ("total_tokens", "total"),
)


def tenant_label(tenant_id: UUID | str | None) -> str:
    return str(tenant_id) if tenant_id is not None else ""


def route_path(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        self.requests = Counter(
            "apipi_requests_total",
            "HTTP requests",
            ["tenant", "method", "path", "status"],
            registry=self.registry,
        )
        self.turns = Counter(
            "apipi_turns_total",
            "Turns",
            ["tenant", "status"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "apipi_tokens_total",
            "Tokens",
            ["tenant", "kind"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "apipi_turn_latency_seconds",
            "Turn latency in seconds",
            ["tenant"],
            registry=self.registry,
            buckets=_LATENCY_BUCKETS,
        )
        self.errors = Counter(
            "apipi_errors_total",
            "Errors",
            ["tenant", "code"],
            registry=self.registry,
        )
        self.usage_export = Counter(
            "apipi_usage_export_total",
            "Usage export attempts",
            ["result"],
            registry=self.registry,
        )
        self.payload_export = Counter(
            "apipi_payload_export_total",
            "Payload export attempts",
            ["result"],
            registry=self.registry,
        )

    def observe_request(
        self,
        *,
        tenant: str,
        method: str,
        path: str,
        status: int,
        error_code: str | None = None,
    ) -> None:
        self.requests.labels(
            tenant=tenant, method=method, path=path, status=str(status)
        ).inc()
        if status >= 400:
            code = error_code if error_code else str(status)
            self.errors.labels(tenant=tenant, code=code).inc()

    def observe_turn(
        self,
        *,
        tenant: str,
        status: str,
        latency_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        total_tokens: int,
        error_code: str | None = None,
    ) -> None:
        self.turns.labels(tenant=tenant, status=status).inc()
        self.latency.labels(tenant=tenant).observe(max(latency_ms, 0) / 1000.0)
        counts = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "total_tokens": total_tokens,
        }
        for field, kind in _TOKEN_KINDS:
            self.tokens.labels(tenant=tenant, kind=kind).inc(counts[field])
        if error_code:
            self.errors.labels(tenant=tenant, code=error_code).inc()

    def observe_usage_export(self, result: str) -> None:
        self.usage_export.labels(result=result).inc()

    def observe_payload_export(self, result: str) -> None:
        self.payload_export.labels(result=result).inc()

    def scrape(self) -> bytes:
        return generate_latest(self.registry)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _SKIP:
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


def observe_turn(
    metrics: Metrics | None,
    *,
    tenant_id: UUID,
    status: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    total_tokens: int,
    error_code: str | None = None,
) -> None:
    if not isinstance(metrics, Metrics):
        return
    metrics.observe_turn(
        tenant=tenant_label(tenant_id),
        status=status,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=total_tokens,
        error_code=error_code,
    )
