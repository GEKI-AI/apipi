# Usage and observability

Tokens now. Dollar cost later. Postgres is the source of truth.
Prometheus and OpenTelemetry are exports. See
[0008](decisions/0008-usage-observability.md).

Never store prompt or completion text in logs, metrics, or spans.

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

Every turn, in every config, appends one Postgres row. Not optional.
Config may add exports. It must not enable full prompt logging. A
setting that would store prompt or completion bodies is rejected or
ignored. Startup says which exports are on.

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

- Echo `x-request-id`. Generate if missing.
- Honor `X-Client-Request-Id` when present (ASCII, ≤512). That value
  becomes the request id.
- The turn log stores the id.

## Query

Tenant-scoped. Wrong tenant is `404`. Tokens and turn counts, not USD.

| Method | Path |
| --- | --- |
| `GET` | `/v1/usage` |

Query params (one of):

| Param | What |
| --- | --- |
| `session_id` | Totals for that session |
| `turn_id` | Totals for that turn |
| `day` | Totals for that UTC day (`YYYY-MM-DD`) |

## Prometheus

`GET /metrics` when `APIPI_METRICS` is on. Off by default.

Series: requests, turns, tokens, latency, errors. Label by tenant
where it is safe. Never by prompt or completion text.

## OpenTelemetry

Export OTLP when `APIPI_OTEL_ENDPOINT` is set. Spans for session, turn,
and the upstream model call. Attributes: request id, session, turn,
model, status, token counts, tool names. Not message text.

## Config

| Config | Default | What |
| --- | --- | --- |
| `APIPI_METRICS` | off | Prometheus at `/metrics` |
| `APIPI_OTEL_ENDPOINT` | unset | OTLP export when set |

The turn log is always on. There is no flag that writes prompt or
completion bodies.

## Compatibility

`/v1/chat/completions` is not a product surface. We do not serve it.

Compatibility tests are HTTP fixtures against [api.md](api.md). A slow
SDK smoke against the official OpenAI Agents SDK is local-only.
