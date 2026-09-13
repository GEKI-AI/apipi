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
exports of the same non-text facts.

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
| `run_mode` | `host` \| `jail` \| `microvm` |
| `instance_id` | Process name, if set |
| `artifact_bytes` | Bytes published this turn |
| `request_id` | Request id |
| `error_code` | Public error code, if any |
| `created_at` | When the usage row was written |

Reads are tenant-scoped. The object must not contain message text.

## Request ids

Every public request except `/health` has an id.

- Echo `x-request-id`. Generate a UUID if that header is missing.
- Honor `X-Client-Request-Id` when present (ASCII, at most 512
  characters). That value becomes the request id.
- The usage event stores the id.
- Authenticated responses include `X-Tenant-Id` and `X-User-Id`.
- When a trace is known, responses include `X-Trace-Id`.

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

This is the path for long-term SaaS analytics. It is not LLM-call
tracing. Existing `APIPI_OTEL_ENDPOINT` stays traces, without bodies.

## Payload export

Set `APIPI_PAYLOAD_EXPORT_URL` to POST one JSON agent payload per
turn. Off when unset (the default). This is session text and tool
arguments/results as items on that turn, not individual LLM API
calls. Join to usage events with `tenant_id`, `session_id`,
`turn_id`, and `request_id`. Retention and PII policy live in the
external tool.

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

`tenant` is the tenant id. Empty when the request has no tenant.
`path` is the route template, not the raw URL. `kind` is `prompt`,
`completion`, `cache_read`, `cache_write`, or `total`. Turn `status`
is `completed`, `failed`, or `cancelled`. Never prompt or completion
text.

## OpenTelemetry

Export OTLP/HTTP traces when `APIPI_OTEL_ENDPOINT` is set. `/v1/traces`
is appended when missing. Spans exist for session, turn, and the
upstream model call. Attributes: request id, session, turn, model,
status, token counts, tool names. Not message text. Not a warehouse
for agent usage history.

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
| `APIPI_OTEL_ENDPOINT` | unset | OTLP traces when set |

The full setting list is in [configuration](config.md).

There is no flag that writes prompt or completion bodies into ApiPi
Postgres, logs, metrics, or default spans.

## Compatibility

`/v1/chat/completions` is not a product surface. We do not serve it.
