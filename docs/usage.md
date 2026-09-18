# Usage and observability

ApiPi records **agent-layer** usage: sessions, turns, tools, MCP,
environment, run mode, latency, artifact bytes, and turn-level token
totals when the harness reports them. It does not trace individual LLM
API calls. That belongs on the model host. It does not store USD.
Operators convert tokens and counters later.

Never store prompt or completion text in the usage tables, logs,
metrics, or default spans. A setting that would write those bodies
into ApiPi Postgres is rejected at startup. Payload bodies, if you
need them, go to an optional external HTTPS export.

Postgres is the **hot** store: recent turns and daily rollups for
quotas and `GET /v1/usage`. Long-term analytics go through an optional
HTTPS usage export. Prometheus and OpenTelemetry traces are local
exports of the same non-text facts. How operators collect those
signals is in [observability](observability.md).

## Tokens

A completed turn may include `usage`. Tokens only. No USD. No message
text.

```json
{
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "total_tokens": 0
}
```

`agent.session.turn.completed` may include that object. `GET` turn
returns it. Missing counts are `0`. Those totals are a turn rollup,
not a substitute for provider billing traces.

## Postgres depth

`APIPI_USAGE_STORE` chooses how much usage lands in Postgres.

| Value | What |
| --- | --- |
| `turns` (default) | One turn log row plus a daily tenant rollup. |
| `rollups` | Daily tenant rollup only. No per-turn rows. |
| `off` | Write nothing to usage tables. |

`APIPI_USAGE_RETENTION` (default `15d`) deletes **turn log** rows older
than that. Empty means no purge. Rollups are not purged; one row per
tenant per UTC day stays small. After turn rows expire, `GET /v1/usage`
by `session_id` or `turn_id` only sees what is still hot. `day` still
reads the rollup.

Startup logs the store, the retention, and whether usage and payload
export are on.

### Agent usage event

Each completed, failed, or cancelled turn emits this non-text object.
Postgres stores a subset depending on depth. The HTTPS export, when
on, POSTs the full object.

| Field | What |
| --- | --- |
| `tenant_id` | Tenant |
| `key_id` | Auth key id |
| `user_id` | Auth `user_id` when the plugin provides it. Not `key_id`. |
| `session_id` | Session |
| `turn_id` | Turn |
| `agent_id` | Agent, if any |
| `model` | Model id (label only) |
| `status` | `completed` \| `failed` \| `cancelled` |
| `latency_ms` | Turn duration (compute proxy) |
| `prompt_tokens` | Prompt tokens |
| `completion_tokens` | Completion tokens |
| `cache_read_tokens` | Cache read tokens |
| `cache_write_tokens` | Cache write tokens |
| `total_tokens` | Sum |
| `tool_names` | Function tool names used |
| `tool_counts` | Calls per function tool |
| `mcp_names` | MCP server labels used |
| `mcp_counts` | Calls per MCP server |
| `environment_type` | `none` \| `openai_hosted` \| `self_hosted` |
| `run_mode` | `none` \| `microvm` \| custom backend `name` |
| `instance_id` | Process name, if set |
| `artifact_bytes` | Bytes published this turn |
| `request_id` | Request id |
| `error_code` | Public error code, if any |
| `created_at` | When the usage row was written |

Reads are tenant-scoped. The object must not contain message text.

Join warehouse rows with `tenant_id`, `user_id`, `agent_id`,
`session_id`, `turn_id`, and `request_id`. Prometheus labels stay
low-cardinality: `tenant` is allowed; `user_id` and `session_id` are
not Prometheus labels. Per-user and per-agent totals come from the
HTTPS usage export (or extra sinks), not from `GET /v1/usage`. That
query is tenant-scoped session, turn, or day rollups only.

## Request ids

Every public request except `/health` has an id.

- Echo `x-request-id`. Generate a UUID if that header is missing.
- Honor `X-Client-Request-Id` when present (ASCII, at most 512
  characters). That value becomes the request id.
- The usage event stores the id.
- Authenticated responses include `X-Tenant-Id` and `X-User-Id`.
- When a trace is known, responses include `X-Trace-Id`.

## Logs

API and worker processes write the same JSON line shape on stderr
(`timestamp`, `level`, `logger`, `message`, `service`). Error and
warning lines that operators should alert on also set `event` and
`error_code`, plus `request_id`, `tenant_id`, `session_id`, `turn_id`,
and `worker_id` when those ids are known. Prompt and completion bodies
are never logged.

| `event` | Level | When |
| --- | --- | --- |
| `turn.failed` | error | A turn failed. `error_code` is the public turn code. |
| `api.error` | error | HTTP 5xx or an unexpected exception. |
| `sandbox.boot.failed` | error | MicroVM jailer or vsock attach failed. |
| `worker.command.failed` | error | A worker command raised after assign. |
| `worker.assign.failed` | warning | No worker capacity (`capacity` or `capacity_tenant`). |
| `worker.lease.expired` | warning | A worker lease TTL elapsed. |
| `usage.export.dropped` | warning | Usage HTTPS export or sink dropped the event. |
| `payload.export.dropped` | warning | Payload HTTPS export or sink dropped the event. |

Failed turns use level `error`. Completed and cancelled turns stay
`info` with `event` `turn`. HTTP request lines stay `info` and include
`error_code` when the response is an ApiPi error. Ship stderr with a
log collector; ApiPi does not bundle Grafana or Loki.

## Query

Tenant-scoped. Wrong tenant is `404`. Tokens and turn counts, not USD.
The numbers come from whatever hot data the store still has.

| Method | Path |
| --- | --- |
| `GET` | `/v1/usage` |

Query params (exactly one of):

| Param | What |
| --- | --- |
| `session_id` | Totals from remaining turn log rows for that session |
| `turn_id` | Totals for that turn log row, or zeros if the turn exists but the log was not kept |
| `day` | Totals for that UTC day (`YYYY-MM-DD`), from the rollup if present, else remaining turn rows |

Missing or more than one param is `400`. Unknown `session_id` or
`turn_id` is `404`. A day with no rows is zeros.

```json
{
  "prompt_tokens": 0,
  "completion_tokens": 0,
  "cache_read_tokens": 0,
  "cache_write_tokens": 0,
  "total_tokens": 0,
  "turns": 0
}
```

`turns` is the number of turn log rows still in Postgres, or the
rollup count for `day`. Missing token counts are `0`. No USD. No
message text.

## Usage export

Set `APIPI_USAGE_EXPORT_URL` to POST one JSON agent usage event per
turn. Off when unset. Put `APIPI_USAGE_EXPORT_TOKEN` in the process
environment; the gateway sends `Authorization: Bearer`. Timeout
defaults to `5s`. After the first try plus `APIPI_USAGE_EXPORT_RETRIES`
(default 1), the event is dropped. A failed export does not change the
session transcript. `apipi_usage_export_total` counts `ok` and `drop`
when Prometheus is on.

This is the path for long-term SaaS analytics.
`APIPI_OTEL_ENDPOINT` stays traces, without bodies.

Extra usage sinks use `APIPI_USAGE_SINKS` (TOML `usage_sinks`): a
comma-separated list of `package.mod:Class`. Each sink implements
`emit(event)` with the same non-text usage object. A missing import
fails at startup. A failed `emit` is logged and does not break the
turn. The HTTPS URL, when set, is one sink on that list.

## Payload export

Set `APIPI_PAYLOAD_EXPORT_URL` to POST one JSON agent payload per
turn. Off when unset (the default). This is session text and tool
arguments/results as items on that turn, not individual LLM API
calls. Join to usage events with `tenant_id`, `session_id`,
`turn_id`, and `request_id`. Retention and PII policy live in the
external tool. Extra payload sinks use `APIPI_PAYLOAD_SINKS` the same
way as usage sinks.

```json
{
  "tenant_id": "...",
  "session_id": "...",
  "turn_id": "...",
  "request_id": "...",
  "items": [
    {"type": "message", "role": "user", "content": "..."},
    {"type": "function_call", "call_id": "...", "name": "...", "arguments": {}},
    {"type": "message", "role": "assistant", "content": "..."}
  ]
}
```

Configured model API keys and export tokens are replaced with
`[redacted]`. `Authorization: Bearer` values are redacted the same
way. Timeout, retries, and drop policy match usage export
(`APIPI_PAYLOAD_EXPORT_TIMEOUT`, `APIPI_PAYLOAD_EXPORT_RETRIES`). A
failed export does not change the session transcript.
`apipi_payload_export_total` counts `ok` and `drop` when Prometheus
is on.

## Prometheus

`GET /metrics` when `APIPI_METRICS` is on. Off by default. No bearer.
Prometheus text format. `/health` and `/metrics` are not counted.

| Series | Type | Labels |
| --- | --- | --- |
| `apipi_requests_total` | counter | `tenant`, `method`, `path`, `status` |
| `apipi_turns_total` | counter | `tenant`, `status` |
| `apipi_tokens_total` | counter | `tenant`, `kind` |
| `apipi_turn_latency_seconds` | histogram | `tenant` |
| `apipi_errors_total` | counter | `tenant`, `code` |
| `apipi_usage_export_total` | counter | `result` (`ok` or `drop`) |
| `apipi_payload_export_total` | counter | `result` (`ok` or `drop`) |
| `apipi_workers` | gauge | connected sandbox workers |
| `apipi_worker_leases` | gauge | active session leases |
| `apipi_worker_assign_seconds` | histogram | time to assign a lease |
| `apipi_worker_capacity` | gauge | advertised session slots on this worker |
| `apipi_worker_sessions` | gauge | live sandboxes on this worker |
| `apipi_worker_memory_mib_used` | gauge | reserved guest RAM in use |
| `apipi_worker_memory_mib_total` | gauge | advertised guest RAM budget |
| `apipi_worker_lease_hold_seconds` | histogram | how long a sandbox stayed live |
| `apipi_sandbox_boot_total` | counter | `size`, `result` (`ok` or `error`) |
| `apipi_sandbox_destroy_total` | counter | `size` |
| `apipi_sandbox_boot_seconds` | histogram | `size` |
| `apipi_sandboxes_active` | gauge | `size` |
| `apipi_guest_memory_bytes` | gauge | jailer cgroup `memory.current`, `size` |
| `apipi_guest_memory_limit_bytes` | gauge | jailer cgroup `memory.max`, `size` |
| `apipi_guest_cpu_seconds` | gauge | jailer cgroup CPU usage, `size` |
| `apipi_guest_mem_available_bytes` | gauge | vsock sample MemAvailable, `size` |
| `apipi_guest_load` | gauge | vsock sample load average, `size` |
| `apipi_guest_workspace_used_bytes` | gauge | vsock sample workspace used, `size` |
| `apipi_guest_workspace_avail_bytes` | gauge | vsock sample workspace free, `size` |

`tenant` is the tenant id. Empty when the request has no tenant.
`path` is the route template, not the raw URL. `kind` is `prompt`,
`completion`, `cache_read`, `cache_write`, or `total`. Turn `status`
is `completed`, `failed`, or `cancelled`. Never prompt or completion
text.

Turn, token, and latency series are recorded once, on the process that
completes the turn. Combined `apipi serve` exposes them on API
`GET /metrics`. With `apipi serve --api-only` plus `apipi worker`, set
`APIPI_METRICS` on the worker so those series are recorded there, and
scrape the worker at `http://<worker>:9091/metrics` (or
`APIPI_WORKER_METRICS_PORT`). The API process still has HTTP request
series and worker-pool gauges (`apipi_workers`, `apipi_worker_leases`,
`apipi_worker_assign_seconds`). It does not double-count turns.

Guest resource series stay low-cardinality (`size` is `S` / `M` /
`L`). They never use `session_id` or `user_id` as labels.

| Layer | What | Default | How |
| --- | --- | --- | --- |
| Host / cgroup | Guest RAM and CPU from the jailer cgroup | On when worker metrics are on | Read on the host. No guest code. |
| Guest sample | MemAvailable, load, workspace disk | Off | Tiny JSON over vsock. Set `APIPI_GUEST_SAMPLE_INTERVAL` (for example `15s`). |
| In-guest Prometheus | node_exporter or a metrics port on TAP | Out of scope | Not lightweight. |

Scrape node_exporter on the worker host if you need machine disk and NIC. ApiPi does not replace that.

## OpenTelemetry

Export OTLP/HTTP traces when `APIPI_OTEL_ENDPOINT` is set. `/v1/traces`
is appended when missing. Spans are sparse and wait-focused. Attributes:
request id, session, turn, model, status, token counts, tool names. Not
message text. Not a warehouse for agent usage history.

| Span | What wait |
| --- | --- |
| `session` | Attach and the request that owns the turn |
| `worker.assign` | Lease / capacity wait before work starts |
| `sandbox.boot` | Cold microVM or Pi spawn |
| `sandbox.attach` | Reuse an already live sandbox |
| `turn` | End-to-end user wait for that turn |
| `model` | Upstream model call |

Inbound `traceparent` is honored and becomes the parent of `session`.
Responses still echo `X-Trace-Id`. Combined `apipi serve` emits the
full tree in one process. In split mode the API emits `session` and
`worker.assign`; set `APIPI_OTEL_ENDPOINT` on the worker for
`sandbox.*`, `turn`, and `model`. The worker command carries
`traceparent` so those spans stay on the same trace.

Use traces to see where time went on a slow turn. Use Prometheus for
rates and saturation. Use JSON logs for error codes and alerts. Use
the usage export for who used how many tokens.

## Config

| Config | Default | What |
| --- | --- | --- |
| `APIPI_USAGE_STORE` | `turns` | `off` \| `rollups` \| `turns` |
| `APIPI_USAGE_RETENTION` | `15d` | Purge turn log rows older than this. Empty = no purge. |
| `APIPI_USAGE_EXPORT_URL` | unset | HTTPS POST of one usage event per turn |
| `APIPI_USAGE_EXPORT_TOKEN` | unset | Bearer for that URL |
| `APIPI_USAGE_EXPORT_TIMEOUT` | `5s` | Export HTTP timeout |
| `APIPI_USAGE_EXPORT_RETRIES` | `1` | Extra tries, then drop |
| `APIPI_PAYLOAD_EXPORT_URL` | unset | HTTPS POST of one agent payload per turn |
| `APIPI_PAYLOAD_EXPORT_TOKEN` | unset | Bearer for that URL |
| `APIPI_PAYLOAD_EXPORT_TIMEOUT` | `5s` | Payload HTTP timeout |
| `APIPI_PAYLOAD_EXPORT_RETRIES` | `1` | Extra tries, then drop |
| `APIPI_METRICS` | off | Prometheus at `/metrics` |
| `APIPI_WORKER_METRICS_HOST` | `0.0.0.0` | Worker scrape bind address |
| `APIPI_WORKER_METRICS_PORT` | `9091` | Worker scrape port |
| `APIPI_GUEST_SAMPLE_INTERVAL` | unset | Opt-in vsock guest samples |
| `APIPI_OTEL_ENDPOINT` | unset | OTLP traces when set |

The full setting list is in [configuration](config.md).

Prompt and completion bodies stay out of ApiPi Postgres, logs, metrics,
and default spans.
