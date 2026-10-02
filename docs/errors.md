# Failure codes

A failed turn is not one error. The model host, the caller, and ApiPi
itself fail in different ways. Each classified failure has
`failure_source`, `code`, `upstream_status`, and `retryable`.

`failure_source` is `upstream` (the model host), `user` (the caller),
or `internal` (ApiPi or the sandbox). `upstream_status` is the HTTP
status parsed from Pi's error text, or null. `retryable` is a hint for
the client. ApiPi does not retry the turn. Only Pi retries model
calls. The model host and the gateway do not. ApiPi writes Pi's retry
and idle timeout into `settings.json`. See [Pi](config.md#pi).

Pi retries a failed assistant message when the error text matches its
retry patterns: timeouts, `429`, `500`–`504`, `524`, connection
errors, and overloaded. It does not retry other `4xx`. ApiPi classifies
the turn only after the last attempt. An earlier `agent_end` with
`willRetry` does not fail the turn. `upstream_attempts` is how many
model attempts ran (`1` when Pi did not retry). It is on
`agent.session.turn.failed`, `agent.session.error`, the `turn.failed`
log, the turn log, and the usage row.

While Pi waits to retry, the client sees
`agent.session.turn.retrying`, then
`agent.session.turn.retry.completed`. Provider-level retries
(`APIPI_MODEL_PROVIDER_RETRIES`) are silent. They are not included in
`upstream_attempts`, and they default to off. Total HTTP tries can
then be up to `(provider retries + 1) × (max retries + 1)`.

A user cancel during the retry wait is
`agent.session.turn.cancelled`, not an upstream failure. A turn that
exceeds `turn_timeout` during retries is `turn_timeout`, with
`upstream_attempts` so far.

Pi does not send the status as a field. ApiPi reads it from
`errorMessage` for the pinned Pi version. A leading `429 …` or
`503: …` is the OpenAI-compatible form. A parenthesized `(401)` is the
prefixed form. Statuses outside 400–599 are ignored. Secrets in the
text are masked. If the text cannot be classified, the code is
`upstream_error` and `upstream_status` is null.

## Where the fields appear

`agent.session.turn.failed` carries the specific `code`.
`agent.session.turn.cancelled` stays a cancel. It adds
`failure_source: user`, `code: cancelled`, and `reason: user`.

`agent.session.error` for a turn failure, and the non-stream `502`
body, carry the specific `code` (copied in `detail_code`). `legacy_code`
is `model_host_error` on those failures. Set `APIPI_ERROR_CODES=legacy`
to keep `model_host_error` in `code` for one release. The next minor
release drops `legacy_code`.

Logs and the usage event use the specific code in `error_code`, plus
`failure_source`, `upstream_status`, `retryable`, and `legacy_code`.
`model_host_error` remains the documented category for
`failure_source == upstream`.

`capacity` and `capacity_tenant` are still HTTP `429` rejections. They
are not turn failures.

`client_disconnected` is reserved for a future opt-in that would
cancel a turn when the streaming client goes away. Turns keep running
after a disconnect today. That mode is not implemented.

## Codes

| Code | Source | When | Retryable | Log level |
| --- | --- | --- | --- | --- |
| `upstream_rate_limited` | upstream | Status 429, or rate-limit text | yes | warning |
| `upstream_5xx` | upstream | Status 500–599, including 524 with a body | yes | error |
| `upstream_timeout` | upstream | Timeout text, 408, or 504 with no body or with timeout text | yes | error |
| `upstream_connection` | upstream | Connection, DNS, or `network_error`, no status | yes | error |
| `upstream_unauthorized` | upstream | 401 or 403 | no | warning |
| `context_length_exceeded` | upstream | 400 or 413, or overflow text, matching Pi's overflow patterns | no | warning |
| `upstream_content_filter` | upstream | `Provider finish_reason: content_filter` | no | warning |
| `upstream_4xx` | upstream | Any other 4xx | no | warning |
| `upstream_error` | upstream | Pi reported an error that cannot be classified | no | error |
| `cancelled` | user | Cancel input. Event is `turn.cancelled`, not `turn.failed` | no | info |
| `client_disconnected` | user | Reserved. Not emitted today | no | info |
| `invalid_request` | user | Request rejected. Specific codes such as `model_required` and `model_not_found` keep their names | no | warning |
| `tool_not_allowed` | user | Tool type not allowed for `environment.type=none` (only function tools, HTTP MCP with `server_url`, and `web_search`) | no | warning |
| `search_not_configured` | user | Agent create or update with a `web_search` tool, and the operator has not configured search for the caller. Returned as `400` | no | warning |
| `builtin_tools` | user | `apipi.builtin_tools=on` for `environment.type=none`, or `apipi.codemode` `on`/`only` with built-in tools off | no | warning |
| `turn_timeout` | internal | `turn_timeout` exceeded | yes | error |
| `pi_exited` | internal | Pi stream ended without `agent_settled` | yes | error |
| `pi_memory` | internal | Host Pi killed by `APIPI_PI_MEM_MIB` | no | error |
| `spawn_failed` | internal | Pi failed to start | yes | error |
| `sandbox_boot_failed` | internal | MicroVM jailer or vsock attach failed. Log only | yes | error |
| `artifact_store` | internal | Object store failure during the turn | yes | error |
| `worker_lease_expired` | internal | Worker lease elapsed | yes | error |
| `turn_interrupted` | internal | Restart left a turn `in_progress` | yes | error |
| `internal` | internal | Unexpected exception | no | error |

Request-time HTTP errors that are not turn failures keep their codes.
`capacity` and `capacity_tenant` are `429`. `payload_too_large` is
`413`. Auth `unauthorized` is `401`. Their `failure_source` is `user`
when a worker command logs them. They are warning, not error.

## Events

A user cancel:

```json
{
  "type": "agent.session.turn.cancelled",
  "data": {
    "turn_id": "…",
    "failure_source": "user",
    "code": "cancelled",
    "reason": "user",
    "upstream_status": null,
    "retryable": false
  }
}
```

An upstream `429`, with `APIPI_ERROR_CODES=specific` (the default):

```json
{
  "type": "agent.session.turn.failed",
  "data": {
    "turn_id": "…",
    "message": "429 Rate limit reached",
    "code": "upstream_rate_limited",
    "failure_source": "upstream",
    "upstream_status": 429,
    "retryable": true,
    "legacy_code": "model_host_error",
    "upstream_attempts": 1
  }
}
```

The matching `agent.session.error` uses `code: upstream_rate_limited` and
`detail_code: upstream_rate_limited`, with `legacy_code: model_host_error`. The `502` body copies those
fields. With `APIPI_ERROR_CODES=legacy`, `code` there stays `model_host_error`.
