# Usage and observability

Tokens now. Dollar cost later. Postgres is the source of truth.
Prometheus and OpenTelemetry are exports.

Never store prompt or completion text in logs, metrics, or spans. A
setting that would store those bodies is rejected at startup.

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
returns it. Missing counts are `0`.

## Turn log

Every turn, in every config, appends one Postgres row. That write is
not optional. Config may add Prometheus or OTLP exports. It must not
enable full prompt logging. Startup logs that the turn log is on, and
whether metrics and OTel are on.

| Field | What |
| --- | --- |
| `tenant_id` | Tenant |
| `session_id` | Session |
| `turn_id` | Turn |
| `agent_id` | Agent, if any |
| `model` | Model id |
| `status` | `completed` \| `failed` \| `cancelled` |
| `latency_ms` | Turn duration |
| `prompt_tokens` | Prompt tokens |
| `completion_tokens` | Completion tokens |
| `cache_read_tokens` | Cache read tokens |
| `cache_write_tokens` | Cache write tokens |
| `total_tokens` | Sum |
| `error_code` | Public error code, if any |
| `request_id` | Request id |
| `tool_names` | Function tool names used |
| `tool_counts` | Calls per function tool |
| `mcp_names` | MCP server labels used |
| `mcp_counts` | Calls per MCP server |

Reads are tenant-scoped. The row must not contain message text.

## Request ids

Every public request except `/health` has an id.

- Echo `x-request-id`. Generate a UUID if that header is missing.
- Honor `X-Client-Request-Id` when present (ASCII, at most 512
  characters). That value becomes the request id.
- The turn log stores the id.

## Query

Tenant-scoped. Wrong tenant is `404`. Tokens and turn counts, not USD.

| Method | Path |
| --- | --- |
| `GET` | `/v1/usage` |

Query params (exactly one of):

| Param | What |
| --- | --- |
| `session_id` | Totals for that session |
| `turn_id` | Totals for that turn |
| `day` | Totals for that UTC day (`YYYY-MM-DD`) |

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

`turns` is the number of turn log rows. Missing token counts are `0`.
No USD. No message text.

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

`tenant` is the tenant id. Empty when the request has no tenant.
`path` is the route template, not the raw URL. `kind` is `prompt`,
`completion`, `cache_read`, `cache_write`, or `total`. Turn `status`
is `completed`, `failed`, or `cancelled`. Never prompt or completion
text.

## OpenTelemetry

Export OTLP/HTTP traces when `APIPI_OTEL_ENDPOINT` is set. `/v1/traces`
is appended when missing. Spans exist for session, turn, and the
upstream model call. Attributes: request id, session, turn, model,
status, token counts, tool names. Not message text.

## Config

| Config | Default | What |
| --- | --- | --- |
| `APIPI_METRICS` | off | Prometheus at `/metrics` |
| `APIPI_OTEL_ENDPOINT` | unset | OTLP export when set |

The full setting list is in [configuration](config.md).

The turn log is always on. There is no flag that writes prompt or
completion bodies.

## Compatibility

`/v1/chat/completions` is not a product surface. We do not serve it.
