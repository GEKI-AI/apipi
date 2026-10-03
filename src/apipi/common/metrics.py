import contextlib
from uuid import UUID

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

disable_created_metrics()

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
_HEARTBEAT_GAP_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0, 120.0)
_HANDLE_BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)
_LAG_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_BYTES_BUCKETS = (256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216)
_COUNT_BUCKETS = (1, 2, 5, 10, 25, 50, 100, 250, 500)
_TOKEN_KINDS = (
    ("prompt_tokens", "prompt"),
    ("completion_tokens", "completion"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
    ("total_tokens", "total"),
)


def tenant_label(tenant_id: UUID | str | None) -> str:
    return str(tenant_id) if tenant_id is not None else ""


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
        self.lifecycle_export = Counter(
            "apipi_lifecycle_export_total",
            "Lifecycle export attempts",
            ["result"],
            registry=self.registry,
        )
        self.lifecycle_queue_depth = Gauge(
            "apipi_lifecycle_queue_depth",
            "Queued lifecycle export events",
            registry=self.registry,
        )
        self.workers = Gauge(
            "apipi_workers",
            "Connected sandbox workers",
            ["run_mode"],
            registry=self.registry,
        )
        self.worker_leases = Gauge(
            "apipi_worker_leases",
            "Active worker session leases",
            ["run_mode"],
            registry=self.registry,
        )
        self.worker_assign = Histogram(
            "apipi_worker_assign_seconds",
            "Time to assign a worker lease",
            registry=self.registry,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )
        self.worker_protocol = Counter(
            "apipi_worker_protocol_total",
            "Worker protocol handshake and auth outcomes",
            ["event"],
            registry=self.registry,
        )
        self.worker_heartbeat_gap = Histogram(
            "apipi_worker_heartbeat_gap_seconds",
            "Seconds between consecutive worker heartbeats",
            registry=self.registry,
            buckets=_HEARTBEAT_GAP_BUCKETS,
        )
        self.worker_lease_events = Counter(
            "apipi_worker_lease_events_total",
            "Worker lease renewals, releases, and expiries",
            ["event"],
            registry=self.registry,
        )
        self.worker_ingest = Counter(
            "apipi_worker_ingest_total",
            "Worker durable envelopes by type and ingest result",
            ["type", "result"],
            registry=self.registry,
        )
        self.worker_ingest_rejected = Counter(
            "apipi_worker_ingest_rejected_total",
            "Worker durable envelopes rejected by ingest",
            ["reason"],
            registry=self.registry,
        )
        self.worker_connections = Gauge(
            "apipi_worker_connections",
            "Open worker sockets on this replica",
            ["run_mode"],
            registry=self.registry,
        )
        self.worker_connects = Counter(
            "apipi_worker_connects_total",
            "Worker register outcomes",
            ["result"],
            registry=self.registry,
        )
        self.worker_disconnects = Counter(
            "apipi_worker_disconnects_total",
            "Worker socket closes by reason",
            ["reason"],
            registry=self.registry,
        )
        self.worker_messages = Counter(
            "apipi_worker_messages_total",
            "Worker socket messages by direction and type",
            ["direction", "type"],
            registry=self.registry,
        )
        self.worker_message_bytes = Histogram(
            "apipi_worker_message_bytes",
            "Worker socket frame sizes in bytes",
            ["direction", "type"],
            registry=self.registry,
            buckets=_BYTES_BUCKETS,
        )
        self.worker_handle = Histogram(
            "apipi_worker_handle_seconds",
            "Time to handle one inbound worker message",
            ["type"],
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_ingest_batch_seconds = Histogram(
            "apipi_worker_ingest_batch_seconds",
            "Time to commit one worker ingest batch",
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_ingest_batch_size = Histogram(
            "apipi_worker_ingest_batch_size",
            "Envelopes in one worker ingest batch",
            registry=self.registry,
            buckets=_COUNT_BUCKETS,
        )
        self.worker_commands = Counter(
            "apipi_worker_commands_total",
            "Worker command delivery by op and result",
            ["op", "result"],
            registry=self.registry,
        )
        self.worker_command_ack = Histogram(
            "apipi_worker_command_ack_seconds",
            "Time from sending a command to its lease.ack",
            ["op"],
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_forwards = Counter(
            "apipi_worker_forwards_total",
            "Commands forwarded to the API replica that holds the worker socket",
            ["op", "result"],
            registry=self.registry,
        )
        self.worker_forward_seconds = Histogram(
            "apipi_worker_forward_seconds",
            "Time from storing a forward to its result",
            ["op"],
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_forward_failures = Counter(
            "apipi_worker_forward_failures_total",
            "Forwarded commands that failed, by reason",
            ["reason"],
            registry=self.registry,
        )
        self.worker_commands_unacked = Gauge(
            "apipi_worker_commands_unacked",
            "Commands waiting for lease.ack",
            registry=self.registry,
        )
        self.worker_send_queue_depth = Gauge(
            "apipi_worker_send_queue_depth",
            "Frames waiting in the per-connection writers",
            registry=self.registry,
        )
        self.worker_presign = Counter(
            "apipi_worker_presign_total",
            "Artifact presign outcomes",
            ["kind", "result"],
            registry=self.registry,
        )
        self.worker_presign_seconds = Histogram(
            "apipi_worker_presign_seconds",
            "Artifact presign handling time",
            ["kind"],
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.search_requests = Counter(
            "apipi_search_requests_total",
            "web_search outcomes",
            ["provider", "result"],
            registry=self.registry,
        )
        self.search_seconds = Histogram(
            "apipi_search_seconds",
            "web_search provider call latency",
            ["provider"],
            registry=self.registry,
            buckets=_LATENCY_BUCKETS,
        )
        self.search_inflight = Gauge(
            "apipi_search_inflight",
            "Running web_search provider calls",
            registry=self.registry,
        )
        self.event_bus_notify_errors = Counter(
            "apipi_event_bus_notify_errors_total",
            "Failed NOTIFY publishes",
            registry=self.registry,
        )
        self.background_loop_errors = Counter(
            "apipi_background_loop_errors_total",
            "Errors caught in background loops",
            ["loop"],
            registry=self.registry,
        )
        self.background_loop_last_run = Gauge(
            "apipi_background_loop_last_run_timestamp",
            "Unix time of the last finished round of a background loop",
            ["loop"],
            registry=self.registry,
        )
        self.event_loop_lag = Histogram(
            "apipi_event_loop_lag_seconds",
            "Event loop scheduling lag, sampled every second",
            registry=self.registry,
            buckets=_LAG_BUCKETS,
        )
        self.worker_info = Gauge(
            "apipi_worker_info",
            "One series per connected worker",
            ["worker_id", "protocol", "version", "run_mode"],
            registry=self.registry,
        )
        self.worker_connected = Gauge(
            "apipi_worker_connected",
            "1 while the worker socket is up and the handshake is done",
            registry=self.registry,
        )
        self.worker_reconnects = Counter(
            "apipi_worker_reconnects_total",
            "Worker reconnect attempts by reason",
            ["reason"],
            registry=self.registry,
        )
        self.worker_connect_seconds = Histogram(
            "apipi_worker_connect_seconds",
            "Worker dial plus handshake time",
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_outbox_messages = Gauge(
            "apipi_worker_outbox_messages",
            "Unacked envelopes in the worker outbox",
            registry=self.registry,
        )
        self.worker_outbox_bytes = Gauge(
            "apipi_worker_outbox_bytes",
            "Unacked bytes in the worker outbox",
            registry=self.registry,
        )
        self.worker_outbox_oldest = Gauge(
            "apipi_worker_outbox_oldest_seconds",
            "Age of the oldest unacked envelope",
            registry=self.registry,
        )
        self.worker_outbox_full = Counter(
            "apipi_worker_outbox_full_total",
            "Turns failed because the outbox was full",
            registry=self.registry,
        )
        self.worker_ack = Histogram(
            "apipi_worker_ack_seconds",
            "Time from outbox append to cumulative ack",
            registry=self.registry,
            buckets=_LATENCY_BUCKETS,
        )
        self.worker_replayed = Counter(
            "apipi_worker_replayed_total",
            "Envelopes resent after a reconnect",
            registry=self.registry,
        )
        self.worker_spool_write = Histogram(
            "apipi_worker_spool_write_seconds",
            "Disk spool write time",
            registry=self.registry,
            buckets=_HANDLE_BUCKETS,
        )
        self.worker_spool_bytes = Gauge(
            "apipi_worker_spool_bytes",
            "Disk spool size in bytes",
            registry=self.registry,
        )
        self.worker_commands_received = Counter(
            "apipi_worker_commands_received_total",
            "Commands handled by the worker",
            ["op", "result"],
            registry=self.registry,
        )
        self.worker_command_seconds = Histogram(
            "apipi_worker_command_seconds",
            "Worker command dispatch time",
            ["op"],
            registry=self.registry,
            buckets=_LATENCY_BUCKETS,
        )
        self.worker_waiter = Counter(
            "apipi_worker_waiter_total",
            "Worker request and reply waits",
            ["kind", "result"],
            registry=self.registry,
        )
        self.worker_deltas_dropped = Counter(
            "apipi_worker_deltas_dropped_total",
            "Live deltas the worker did not send",
            ["reason"],
            registry=self.registry,
        )
        self.worker_draining = Gauge(
            "apipi_worker_draining",
            "1 while the worker drains",
            registry=self.registry,
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
        self.pi_processes = Gauge(
            "apipi_pi_processes",
            "Live host Pi processes on this worker",
            registry=self.registry,
        )
        self.pi_rss_bytes = Gauge(
            "apipi_pi_rss_bytes",
            "Sum of host Pi process-group RSS",
            registry=self.registry,
        )
        self.pi_pss_bytes = Gauge(
            "apipi_pi_pss_bytes",
            "Sum of host Pi process-group PSS",
            registry=self.registry,
        )
        self.pi_spawn = Counter(
            "apipi_pi_spawn_total",
            "Host Pi spawns",
            ["result"],
            registry=self.registry,
        )
        self.pi_kill = Counter(
            "apipi_pi_kill_total",
            "Host Pi teardowns",
            ["reason"],
            registry=self.registry,
        )
        self.event_bus_reconnects = Counter(
            "apipi_event_bus_listener_reconnects_total",
            "Postgres LISTEN reconnects",
            registry=self.registry,
        )
        self.pg_notification_queue_usage = Gauge(
            "apipi_pg_notification_queue_usage",
            "Postgres notification queue usage (0-1)",
            registry=self.registry,
        )
        self.event_bus_wake_sse = Histogram(
            "apipi_event_bus_wake_sse_seconds",
            "Wake to SSE delivery latency in seconds",
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
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

    def observe_lifecycle_export(self, result: str) -> None:
        self.lifecycle_export.labels(result=result).inc()

    def set_lifecycle_queue_depth(self, depth: int) -> None:
        self.lifecycle_queue_depth.set(depth)

    def set_workers(
        self,
        counts: dict[str, int],
        leases: dict[str, int],
        *,
        modes: set[str],
    ) -> None:
        for mode in modes:
            self.workers.labels(run_mode=mode).set(counts.get(mode, 0))
            self.worker_leases.labels(run_mode=mode).set(leases.get(mode, 0))

    def observe_worker_protocol(self, event: str) -> None:
        self.worker_protocol.labels(event=event).inc()

    def observe_worker_heartbeat_gap(self, seconds: float) -> None:
        self.worker_heartbeat_gap.observe(max(seconds, 0.0))

    def observe_worker_lease_event(self, event: str) -> None:
        self.worker_lease_events.labels(event=event).inc()

    def observe_worker_ingest(self, type: str, result: str) -> None:
        self.worker_ingest.labels(type=type, result=result).inc()

    def observe_worker_ingest_rejected(self, reason: str) -> None:
        self.worker_ingest_rejected.labels(reason=reason).inc()

    def set_worker_connections(
        self, counts: dict[str, int], *, modes: set[str]
    ) -> None:
        for mode in modes:
            self.worker_connections.labels(run_mode=mode).set(counts.get(mode, 0))

    def observe_worker_connect(self, result: str) -> None:
        self.worker_connects.labels(result=result).inc()

    def observe_worker_disconnect(self, reason: str) -> None:
        self.worker_disconnects.labels(reason=reason).inc()

    def observe_worker_message(
        self, direction: str, type: str, size: int | None = None
    ) -> None:
        self.worker_messages.labels(direction=direction, type=type).inc()
        if size is not None:
            self.worker_message_bytes.labels(direction=direction, type=type).observe(
                size
            )

    def observe_worker_handle(self, type: str, seconds: float) -> None:
        self.worker_handle.labels(type=type).observe(max(seconds, 0.0))

    def observe_worker_ingest_batch(self, *, seconds: float, size: int) -> None:
        self.worker_ingest_batch_seconds.observe(max(seconds, 0.0))
        self.worker_ingest_batch_size.observe(size)

    def observe_worker_command(self, op: str, result: str) -> None:
        self.worker_commands.labels(op=op, result=result).inc()

    def observe_worker_forward(self, op: str, result: str, seconds: float) -> None:
        self.worker_forwards.labels(op=op, result=result).inc()
        self.worker_forward_seconds.labels(op=op).observe(max(seconds, 0.0))

    def observe_worker_forward_failure(self, reason: str) -> None:
        self.worker_forward_failures.labels(reason=reason).inc()

    def observe_worker_command_ack(self, op: str, seconds: float) -> None:
        self.worker_command_ack.labels(op=op).observe(max(seconds, 0.0))

    def set_worker_commands_unacked(self, count: int) -> None:
        self.worker_commands_unacked.set(count)

    def set_worker_send_queue_depth(self, depth: int) -> None:
        self.worker_send_queue_depth.set(depth)

    def observe_worker_presign(self, kind: str, result: str, seconds: float) -> None:
        self.worker_presign.labels(kind=kind, result=result).inc()
        self.worker_presign_seconds.labels(kind=kind).observe(max(seconds, 0.0))

    def observe_search(self, provider: str, result: str, seconds: float) -> None:
        self.search_requests.labels(provider=provider, result=result).inc()
        self.search_seconds.labels(provider=provider).observe(max(seconds, 0.0))

    def add_search_inflight(self, delta: int) -> None:
        self.search_inflight.inc(delta)

    def observe_event_bus_notify_error(self) -> None:
        self.event_bus_notify_errors.inc()

    def observe_background_loop_error(self, loop: str) -> None:
        self.background_loop_errors.labels(loop=loop).inc()

    def set_background_loop_last_run(self, loop: str, timestamp: float) -> None:
        self.background_loop_last_run.labels(loop=loop).set(timestamp)

    def observe_event_loop_lag(self, seconds: float) -> None:
        self.event_loop_lag.observe(max(seconds, 0.0))

    def set_worker_info(
        self, *, worker_id: str, protocol: str, version: str, run_mode: str
    ) -> None:
        self.worker_info.labels(
            worker_id=worker_id, protocol=protocol, version=version, run_mode=run_mode
        ).set(1)

    def clear_worker_info(
        self, *, worker_id: str, protocol: str, version: str, run_mode: str
    ) -> None:
        with contextlib.suppress(KeyError):
            self.worker_info.remove(worker_id, protocol, version, run_mode)

    def set_worker_connected(self, connected: bool) -> None:
        self.worker_connected.set(1 if connected else 0)

    def observe_worker_reconnect(self, reason: str) -> None:
        self.worker_reconnects.labels(reason=reason).inc()

    def observe_worker_connect_seconds(self, seconds: float) -> None:
        self.worker_connect_seconds.observe(max(seconds, 0.0))

    def set_worker_outbox(
        self, *, messages: int, size: int, oldest_seconds: float
    ) -> None:
        self.worker_outbox_messages.set(messages)
        self.worker_outbox_bytes.set(size)
        self.worker_outbox_oldest.set(max(oldest_seconds, 0.0))

    def observe_worker_outbox_full(self) -> None:
        self.worker_outbox_full.inc()

    def observe_worker_ack(self, seconds: float) -> None:
        self.worker_ack.observe(max(seconds, 0.0))

    def observe_worker_replayed(self, count: int = 1) -> None:
        self.worker_replayed.inc(count)

    def observe_worker_spool_write(self, seconds: float) -> None:
        self.worker_spool_write.observe(max(seconds, 0.0))

    def set_worker_spool_bytes(self, size: int) -> None:
        self.worker_spool_bytes.set(size)

    def observe_worker_command_received(self, op: str, result: str) -> None:
        self.worker_commands_received.labels(op=op, result=result).inc()

    def observe_worker_command_seconds(self, op: str, seconds: float) -> None:
        self.worker_command_seconds.labels(op=op).observe(max(seconds, 0.0))

    def observe_worker_waiter(self, kind: str, result: str) -> None:
        self.worker_waiter.labels(kind=kind, result=result).inc()

    def observe_worker_delta_dropped(self, reason: str, count: int = 1) -> None:
        self.worker_deltas_dropped.labels(reason=reason).inc(count)

    def set_worker_draining(self, draining: bool) -> None:
        self.worker_draining.set(1 if draining else 0)

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

    def set_host_pi(
        self, *, processes: int, rss_bytes: float, pss_bytes: float
    ) -> None:
        self.pi_processes.set(processes)
        self.pi_rss_bytes.set(rss_bytes)
        self.pi_pss_bytes.set(pss_bytes)

    def observe_pi_spawn(self, result: str) -> None:
        self.pi_spawn.labels(result=result).inc()

    def observe_pi_kill(self, reason: str) -> None:
        self.pi_kill.labels(reason=reason).inc()

    def observe_event_bus_reconnect(self) -> None:
        self.event_bus_reconnects.inc()

    def observe_wake_sse(self, seconds: float) -> None:
        self.event_bus_wake_sse.observe(max(seconds, 0.0))

    def set_pg_notification_queue_usage(self, value: float) -> None:
        self.pg_notification_queue_usage.set(value)

    def scrape(self) -> bytes:
        return generate_latest(self.registry)


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
