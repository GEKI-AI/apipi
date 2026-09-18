from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.gateway.http_path import skip_request_path

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
        self.workers = Gauge(
            "apipi_workers",
            "Connected sandbox workers",
            registry=self.registry,
        )
        self.worker_leases = Gauge(
            "apipi_worker_leases",
            "Active worker session leases",
            registry=self.registry,
        )
        self.worker_assign = Histogram(
            "apipi_worker_assign_seconds",
            "Time to assign a worker lease",
            registry=self.registry,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.worker_capacity = Gauge(
            "apipi_worker_capacity",
            "Advertised session slots on this worker",
            registry=self.registry,
        )
        self.worker_sessions = Gauge(
            "apipi_worker_sessions",
            "Live sandboxes on this worker",
            registry=self.registry,
        )
        self.worker_memory_used = Gauge(
            "apipi_worker_memory_mib_used",
            "Reserved guest RAM in use on this worker",
            registry=self.registry,
        )
        self.worker_memory_total = Gauge(
            "apipi_worker_memory_mib_total",
            "Advertised guest RAM budget on this worker",
            registry=self.registry,
        )
        self.worker_lease_hold = Histogram(
            "apipi_worker_lease_hold_seconds",
            "How long a sandbox stayed live",
            registry=self.registry,
            buckets=_LATENCY_BUCKETS,
        )
        self.sandbox_boot = Counter(
            "apipi_sandbox_boot_total",
            "Sandbox boots",
            ["size", "result"],
            registry=self.registry,
        )
        self.sandbox_destroy = Counter(
            "apipi_sandbox_destroy_total",
            "Sandbox teardowns",
            ["size"],
            registry=self.registry,
        )
        self.sandbox_boot_seconds = Histogram(
            "apipi_sandbox_boot_seconds",
            "Sandbox boot time",
            ["size"],
            registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self.sandboxes_active = Gauge(
            "apipi_sandboxes_active",
            "Live sandboxes by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_memory_current = Gauge(
            "apipi_guest_memory_bytes",
            "Sum of jailer cgroup memory.current by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_memory_limit = Gauge(
            "apipi_guest_memory_limit_bytes",
            "Sum of jailer cgroup memory.max by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_cpu_seconds = Gauge(
            "apipi_guest_cpu_seconds",
            "Sum of jailer cgroup cpu.stat usage by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_mem_available = Gauge(
            "apipi_guest_mem_available_bytes",
            "Sum of guest MemAvailable by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_load = Gauge(
            "apipi_guest_load",
            "Mean guest load average by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_workspace_used = Gauge(
            "apipi_guest_workspace_used_bytes",
            "Sum of guest workspace used bytes by size",
            ["size"],
            registry=self.registry,
        )
        self.guest_workspace_avail = Gauge(
            "apipi_guest_workspace_avail_bytes",
            "Sum of guest workspace free bytes by size",
            ["size"],
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

    def set_worker_util(
        self,
        *,
        capacity: int,
        sessions: int,
        memory_mib_used: int,
        memory_mib_total: int,
    ) -> None:
        self.worker_capacity.set(capacity)
        self.worker_sessions.set(sessions)
        self.worker_memory_used.set(memory_mib_used)
        self.worker_memory_total.set(memory_mib_total)

    def observe_sandbox_boot(self, *, size: str, result: str, seconds: float) -> None:
        self.sandbox_boot.labels(size=size, result=result).inc()
        if result == "ok":
            self.sandbox_boot_seconds.labels(size=size).observe(max(seconds, 0.0))

    def observe_sandbox_destroy(self, *, size: str, hold_seconds: float) -> None:
        self.sandbox_destroy.labels(size=size).inc()
        self.worker_lease_hold.observe(max(hold_seconds, 0.0))

    def set_sandboxes_active(self, counts: dict[str, int]) -> None:
        for size in ("S", "M", "L"):
            self.sandboxes_active.labels(size=size).set(counts.get(size, 0))

    def set_guest_cgroup(
        self,
        *,
        size: str,
        memory_bytes: float,
        memory_limit_bytes: float,
        cpu_seconds: float,
    ) -> None:
        self.guest_memory_current.labels(size=size).set(memory_bytes)
        self.guest_memory_limit.labels(size=size).set(memory_limit_bytes)
        self.guest_cpu_seconds.labels(size=size).set(cpu_seconds)

    def set_guest_sample(
        self,
        *,
        size: str,
        mem_available_bytes: float,
        load: float,
        workspace_used_bytes: float,
        workspace_avail_bytes: float,
    ) -> None:
        self.guest_mem_available.labels(size=size).set(mem_available_bytes)
        self.guest_load.labels(size=size).set(load)
        self.guest_workspace_used.labels(size=size).set(workspace_used_bytes)
        self.guest_workspace_avail.labels(size=size).set(workspace_avail_bytes)

    def scrape(self) -> bytes:
        return generate_latest(self.registry)


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
