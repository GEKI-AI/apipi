# Observability

ApiPi **emits** structured logs, Prometheus metrics, OpenTelemetry
traces, and agent-layer usage events. Operators **collect** those
signals with their own stack. ApiPi does not ship Grafana, Loki,
Tempo, Alertmanager, or Sentry. It does not store USD. It does not
put prompt or completion bodies in Postgres, default logs, metrics,
or spans.

Laptop defaults stay quiet: `APIPI_METRICS` is off and
`APIPI_OTEL_ENDPOINT` is unset. Production turns signals on and
points exporters at the operator collector. What each series and
field means is on [usage](usage.md). This page is how to emit and
collect in production.

## You bring

| Need | You run |
| --- | --- |
| Log store | Vector, Fluent Bit, Grafana Alloy, or a cloud log agent reading stderr |
| Metrics | Prometheus (or compatible) scraping `/metrics` |
| Traces | An OTLP collector, then Tempo, Jaeger, or a vendor |
| Warehouse / BI | HTTPS sink for usage export, then your warehouse |
| Dashboards and alerts | Grafana, Alertmanager, or the vendor UI |

ApiPi does not bundle those tools. Optional example dashboards are
not a runtime dependency.

## What to enable

| Config | Default | Production |
| --- | --- | --- |
| `APIPI_LOG_LEVEL` | `info` | `info` |
| `APIPI_LOG_FORMAT` | `json` | `json` (use `text` only on a laptop) |
| `APIPI_METRICS` | off | on, on **API and worker** |
| `APIPI_WORKER_METRICS_HOST` | `0.0.0.0` | scrape bind on the worker |
| `APIPI_WORKER_METRICS_PORT` | `9091` | worker `/metrics` |
| `APIPI_GUEST_SAMPLE_INTERVAL` | unset (off) | unset, or `15s` if you want vsock samples |
| `APIPI_OTEL_ENDPOINT` | unset | OTLP/HTTP collector, on **API and worker** |
| `APIPI_USAGE_STORE` | `turns` | `turns` or `rollups` |
| `APIPI_USAGE_RETENTION` | `15d` | keep the hot store bounded |
| `APIPI_USAGE_EXPORT_URL` | unset | HTTPS POST of one usage event per turn |
| `APIPI_USAGE_EXPORT_TOKEN` | unset | bearer for that URL |
| `APIPI_PAYLOAD_EXPORT_URL` | unset | only if you need message bodies outside ApiPi |

The full setting list is in [configuration](config.md).

Combined `apipi serve` is one process: scrape its `/metrics` and set
OTEL on that process. Production is `apipi serve --api-only` plus
`apipi worker`. Turn series and turn/model spans are recorded on the
worker. HTTP series and `worker.assign` spans are on the API. Set
`APIPI_METRICS` and `APIPI_OTEL_ENDPOINT` on both.

## Logs

Ship stderr. There is no log file in the gateway. Each line is one
JSON object with `timestamp`, `level`, `logger`, `message`, and
`service` (`apipi`). Error and warning lines that operators should
alert on also set `event` and `error_code`, plus `request_id`,
`tenant_id`, `session_id`, `turn_id`, and `worker_id` when known.

A typical shipper reads stderr and writes Loki, CloudWatch, or
another store. Example shape (Vector):

```toml
[sources.apipi]
type = "stdin"
decoding.codec = "json"

[sinks.loki]
type = "loki"
inputs = ["apipi"]
endpoint = "http://loki:3100"
```

Fluent Bit, Alloy, and cloud agents work the same way: JSON lines on
stderr, no ApiPi-side shipper.

The event table is in [usage](usage.md#logs). Alert on
`turn.failed`, `api.error`, `sandbox.boot.failed`,
`worker.assign.failed`, `worker.lease.expired`, and export drops.

## Prometheus

No bearer. Network-restrict `/metrics` like any scrape endpoint.

| Process | Scrape | What you get |
| --- | --- | --- |
| Combined `apipi serve` | `http://<api>:8000/metrics` | HTTP, turns, tokens, worker-pool gauges, sandbox series if this process runs sandboxes |
| API-only | `http://<api>:8000/metrics` | HTTP requests, errors, `apipi_workers` and `apipi_worker_leases` (labeled `run_mode`), `apipi_worker_assign_seconds` |
| Worker | `http://<worker>:9091/metrics` | Turns, tokens, utilization, sandbox boot/destroy, host Pi RSS/PSS, cgroup guest RAM/CPU, optional vsock samples |

Worker metric sets (same scrape, metrics on):

| Set | When | Series |
| --- | --- | --- |
| Worker util | All run modes | `apipi_worker_{capacity,sessions,memory_mib_*}` |
| Sandbox lifecycle | Any spawn through `PiPool` | `apipi_sandbox_*` |
| Host Pi | `chat` / `none` (no `vm_id`) | `apipi_pi_processes`, `apipi_pi_rss_bytes`, `apipi_pi_pss_bytes`, `apipi_pi_spawn_total`, `apipi_pi_kill_total` |
| MicroVM guest | `vm_id` set | `apipi_guest_*` |

`apipi_worker_memory_mib_used` is reserved guest budget for placement. `apipi_pi_rss_bytes` is actual host Pi RAM (process group, including MCP children Pi started). Guest jailer cgroup is `apipi_guest_memory_bytes`. Do not mix them.

Guest resource layers:

| Layer | What | Default | How |
| --- | --- | --- | --- |
| A. Host / cgroup | Jailer cgroup memory and CPU | On when worker metrics are on | Read on the host. No guest code. |
| B. Guest sample | MemAvailable, load, workspace disk | Off | Tiny JSON over vsock. Set `APIPI_GUEST_SAMPLE_INTERVAL`. |
| C. In-guest Prometheus | node_exporter on TAP | Out of scope | Not lightweight. Attack surface. |

Prometheus labels stay low-cardinality. `tenant` is allowed. Do not
put `session_id` or `user_id` on series. Scrape node_exporter on the
worker host if you need machine disk and NIC.

## Traces

When `APIPI_OTEL_ENDPOINT` is set, ApiPi exports OTLP/HTTP traces.
`/v1/traces` is appended if missing. Spans are wait-focused:
`session`, `worker.assign`, `sandbox.boot`, `sandbox.attach`,
`turn`, `model`. Inbound `traceparent` is honored. The worker
command carries it so split API plus worker stays one trace.

Point the endpoint at your collector (Tempo, Jaeger, or a vendor).
Use traces to see where time went on a slow turn. Use Prometheus for
rates and saturation. Use logs for error codes.

## Usage export

`GET /v1/usage` is the hot store: tenant-scoped session, turn, or
day. Long-term “who used what” is `APIPI_USAGE_EXPORT_URL`: one JSON
event per turn, tokens and counts only, never USD. Join with
`tenant_id`, `user_id` (when the auth plugin set it), `agent_id`,
`session_id`, `turn_id`, and `request_id`.

Auth plugins may return `user_id`. ApiPi does not invent it from
`key_id`. `X-User-Id` on HTTP responses is still `key_id`. See
[auth](auth.md) and [usage](usage.md).

## Suggested alerts

| Signal | Why |
| --- | --- |
| Rate of `event=turn.failed` or `apipi_turns_total{status="failed"}` | Turns dying |
| `apipi_errors_total` 5xx / `event=api.error` | Gateway faults |
| HTTP `429` with `capacity` / `event=worker.assign.failed` | Node or tenant full |
| `apipi_sandbox_boot_total{result="error"}` / `event=sandbox.boot.failed` | Guests not starting |
| `apipi_worker_sessions` near `apipi_worker_capacity` | Packing too tight |
| `apipi_pi_rss_bytes` near host RAM on a chat worker | Dense Pi packing |
| `apipi_worker_assign_seconds` p95 | Lease wait |
| `apipi_usage_export_total{result="drop"}` | Warehouse gaps |
| `event=worker.lease.expired` | Worker died or heartbeat failed |

## Cardinality

| Signal | Identity |
| --- | --- |
| Prometheus | `tenant` ok. Not `user_id` or `session_id`. Guest series use `size` (`S` / `M` / `L`). |
| Logs and traces | `request_id`, `tenant_id`, `session_id`, `turn_id`, `worker_id` when known |
| Usage export | `tenant_id`, `user_id`, `key_id`, `agent_id`, `session_id`, `turn_id`, `request_id` |
