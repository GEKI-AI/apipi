# Usage and observability

ApiPi records **agent-layer** usage: sessions, turns, tools, MCP,
web searches, environment, run mode, latency, artifact bytes, and
turn-level token totals when the harness reports them. It does not trace individual LLM
API calls. That belongs on the model host. It does not store USD.
Operators convert tokens and counters later.

Never store prompt or completion text in the usage tables, logs,
metrics, or default spans. A setting that would write those bodies
into ApiPi Postgres is rejected at startup. Payload bodies, if you
need them, go to an optional external HTTPS export.

Postgres is the **hot** store: recent turns and daily rollups for
`GET /v1/apipi/usage`. Long-term analytics go through an optional
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
tenant per UTC day stays small. After turn rows expire, `GET /v1/apipi/usage`
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
| `search_calls` | Built-in `web_search` calls the provider charged this turn. See [Search](#search). |
| `search_units` | Provider units charged for those calls (credits for Tavily, requests for Staan) |
| `search_counts` | Search calls and units per provider and key source, for example `{"tavily/operator": {"calls": 2, "units": 2}}` |
| `environment_type` | `none` \| `openai_hosted` |
| `run_mode` | `none` \| `microvm` \| custom backend `name` |
| `instance_id` | Process name, if set |
| `artifact_bytes` | Bytes published this turn |
| `request_id` | Request id |
| `error_code` | Specific failure code, if the turn failed or was cancelled |
| `failure_source` | `upstream`, `user`, or `internal`, if classified |
| `upstream_status` | Model-host HTTP status when the text contained one |
| `retryable` | Whether an identical retry may succeed |
| `legacy_code` | `model_host_error` for upstream failures during the migration |
| `upstream_attempts` | Model attempts observed for this turn, when known. `1` when Pi did not retry. Silent provider retries are not included. |
| `created_at` | When the usage row was written |

Reads are tenant-scoped. The object must not contain message text.

Join warehouse rows with `tenant_id`, `user_id`, `agent_id`,
`session_id`, `turn_id`, and `request_id`. Prometheus labels stay
low-cardinality: `tenant` is allowed; `user_id` and `session_id` are
not Prometheus labels. Per-user and per-agent totals come from the
HTTPS usage export (or extra sinks), not from `GET /v1/apipi/usage`. That
query is tenant-scoped session, turn, or day rollups only.

## Search

The built-in [`web_search` tool](tools.md#web-search) is counted by the
API, because the API makes the provider call. The count never comes
from the worker `usage` envelope, so a worker cannot change it.

| Counter | What |
| --- | --- |
| `search_calls` | Search calls the provider charged for. |
| `search_units` | Units the provider charged for those calls. Tavily reports credits (`basic` is 1, `advanced` is 2). Staan charges 1 for each request. |
| `search_counts` | The same two numbers per `<provider>/<key_source>`. `key_source` is `operator` for the key in the API config. A later per-tenant key will be `tenant`, so an operator can bill only the operator-key searches. |

The rule is simple: only calls the provider charged are counted. A
successful call is counted. A failed call is counted only when the
provider says it charged for it. A timeout, a transport error, and a
provider `4xx` or `5xx` response are not counted. A `2xx` response
with a body ApiPi cannot read is counted, because the provider
processed the request. A search that is
denied or invalid before a provider call is not counted. These are
counters, not USD. Convert units to money outside ApiPi.

The numbers appear in several places.

| Where | What |
| --- | --- |
| Turn log | `search_calls`, `search_units`, and `search_counts` for each turn (store depth `turns`). |
| Daily rollup | `search_calls` and `search_units` per tenant per UTC day. |
| `GET /v1/apipi/usage` | `search_calls` and `search_units` for `session_id`, `turn_id`, or `day`. |
| Usage event | `search_calls`, `search_units`, and `search_counts`. The HTTPS export and extra sinks get them with `user_id` and `org_id`, so search can be billed per subject. |

The API records a search when it answers the request, which is before
the turn ends. When the turn row is written later, it takes the
counts already recorded for that turn. If the turn row already exists
when a search is recorded, the counts are added to that row and to the
day's rollup. Either order gives the same totals. Logs for a search
carry the query length, the provider, the status, and the latency.
They never carry the query text or the key.

The per-turn search counts are kept even when `APIPI_USAGE_STORE` is
`off`, because the usage event and the export need them. They are
removed with the turn logs after `APIPI_USAGE_RETENTION`. With the
`rollups` store depth there is no turn row to mark a finished turn, so
a search recorded after the turn's usage was written is not added to
the rollup. The API refuses a search for a turn that is no longer
running, so this does not happen in normal use.

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
and `worker_id` when those ids are known. Lines of a worker connection
also carry `connection_id`. Prompt and completion bodies, command
contexts, keys, presigned URLs, and search queries are never logged.
Warnings that repeat are written once, then at most once per minute with
a `count` of the occurrences since the last line.

| `event` | Level | When |
| --- | --- | --- |
| `turn.failed` | warning or error | A turn failed. `error_code` is the specific code. `failure_source`, `upstream_status`, `retryable`, `legacy_code`, and `upstream_attempts` are set when known. Caller and `429` failures are warning. Upstream `5xx`, timeouts, connection errors, and internal failures are error. See [failure codes](errors.md). |
| `api.error` | error | HTTP 5xx or an unexpected exception. |
| `sandbox.boot.failed` | error | MicroVM jailer or vsock attach failed. `error_code` is `sandbox_boot_failed`. |
| `worker.command.failed` | warning or error | A worker command raised. Caller errors are warning. Internal faults are error. Fields include `session_id`, `tenant_id`, and `request_id`. Turn commands also emit a session failure event unless the session is already `failed`. |
| `worker.assign.failed` | warning | No worker capacity (`capacity` or `capacity_tenant`). |
| `worker.lease.expired` | error | A worker lease TTL elapsed. `error_code` is `worker_lease_expired`. Also carries `lease_ttl_seconds`, `last_renewal_age_seconds` (time since the lease was last renewed), `last_heartbeat_age_seconds` (time since the last heartbeat on the connected socket, absent when the worker is not connected), and `worker_connected`. |
| `worker.heartbeat.late` | warning | Rate limited. The gap between two heartbeats passed half the lease TTL. `source` is `api` (measured on receipt, so it includes the network) or `worker` (measured on the sending timer). Carries `gap_seconds`, `lease_ttl_seconds`, and `worker_id` on the API side. |
| `worker.event.rejected` | warning | A durable worker envelope was rejected by ingest. `error_code` is the reason (`not_leased`, `turn_mismatch`, and so on). The worker is still acked past it. |
| `worker.ingest.duplicate` | info | A batch contained envelopes whose sequence numbers were already ingested. One line per session and batch, with `count`, `first_seq`, and `last_seq`. Normal after a reconnect replay. |
| `worker.release.unflushed` | warning | The worker waited for the API to ack its buffered envelopes before a `lease.release` or the `session.stop` ack, and gave up after `RELEASE_FLUSH_TIMEOUT` (10 seconds). The release went out anyway. |
| `worker.command.cursor_missing` | warning | A `turn.start`, `turn.continue`, or `sandbox.boot` command carried no valid `last_seq`, so the worker could not continue the session sequence from the API cursor. |
| `worker.lease_ttl.ignored` | warning | `APIPI_WORKER_LEASE_TTL` is set on a worker. The API sets the TTL, so the worker ignores it. |
| `search.usage_failed` | error | The API could not store the usage of a search after three tries. The model still gets the results. Carries the provider, the key source, and the counts, so the numbers can be added by hand. |
| `search.denied` | warning | An agent has the `web_search` tool but search is not allowed for the session, so the tool was not loaded for the turn, or a `search.request` arrived for such a session. Carries no query text. The turn does not fail. |
| `worker.connected` | info | The API accepted a worker register. Carries `worker_id`, `connection_id`, `run_mode`, `version`, and the number of leases it reattached. |
| `worker.hello.sent` | info | The API sent `hello.reply`. Carries `connection_id`, the number of running sessions, and the number of revokes. |
| `worker.hello.received` | info | The worker received `hello.reply`. Carries `connection_id`, the lease TTL, the heartbeat interval, and the connect time. |
| `worker.disconnected` | info | A worker socket closed. On the API it carries `reason` (`clean`, `error`, `ping_timeout`, `write_timeout`, `takeover`, `revoked`, `protocol_violation`, `ingest_failed`), `duration_s`, `leases`, and `unacked_commands`. On the worker it carries `reason` (`closed`, `ping_timeout`, `error`, `drained`, `drain_timeout`), `duration_s`, `unacked_envelopes`, `unacked_sessions`, and `deltas_dropped`. Look at `reason` when many workers disconnect together. |
| `worker.connection.lost` | warning | The worker lost its socket or could not connect. `error_code` is the reconnect reason (`closed`, `connect_error`, `ping_timeout`, `hello_timeout`, `error`). Rate limited. |
| `worker.reconnecting` | info | The worker will reconnect. Carries `reason`, `attempt`, and `delay_s`. |
| `worker.drain.started`, `worker.drain.waiting`, `worker.drain.finished` | info, info, warning | The worker started or finished draining. `waiting` means no Pi is live and the worker waits for the API to ack the outbox. `finished` is a warning with `error_code` `drain_timeout` when sessions were still live or envelopes unacked at the drain timeout, with `sessions`, `unacked_sessions`, and `unacked_envelopes`. |
| `worker.spool.recovered` | info | The worker reloaded its disk outbox at start. Carries the number of sessions, envelopes, and bytes, `skipped_lines` (torn or invalid lines), and `spool_bytes`. |
| `worker.outbox.spool_error` | warning | The disk spool could not be written, compacted, synced, or removed. Carries `path`. Rate limited. |
| `worker.replay` | info | The worker resends unacked envelopes after a reconnect. Carries `envelopes` (sent before and sent again), `sessions`, and `unclaimed_sessions` (spooled sessions the worker did not claim in `register`). |
| `worker.harvest.skipped` | info | A killed session was not harvested because no socket was open. Carries `session_id`. |
| `worker.outbox.oversize` | warning | An envelope was over `MAX_MESSAGE_BYTES` and the turn failed with `worker_message_too_large`. Carries `type` and `size`. Rate limited. |
| `worker.message.failed` | warning | The worker or the API failed to handle one message and went on. On the API it is a database or store error, the socket stays open, and the line carries `type`, `error`, and `transient`. Rate limited per type. |
| `worker.command` | info | The worker accepted a command. Carries `op`, `command_id`, `lease_id`, `request_id`, `traceparent`, and a summary of the context (counts and ids, never keys or text). |
| `worker.lease.granted`, `worker.lease.released`, `worker.lease.revoked`, `worker.lease.taken_over` | info | A lease changed. Carries `lease_id`, `session_id`, `worker_id`, and `reason` where it applies. `worker.lease.expired` and `worker.lease.orphaned` are the error lines for the other two changes. |
| `worker.command.ack_timeout` | warning | The worker did not ack a command in time. Carries `command_id`, `lease_id`, and `op`. Check the worker connection and its event loop. Rate limited. |
| `worker.command.retransmitted` | warning | The API resent an unacked command, after a worker reconnected or on the 5 second timer. Carries `reason` (`reconnect` or `timer`) and `sends`. Rate limited. |
| `worker.command.expired` | error | A command was not acked within the lease TTL. `error_code` is `worker_command_timeout`. The API cleared the lease, failed the turn, and sent `lease.revoke`. Carries `op`, `command_id`, `lease_id`, `sends`, `age_seconds`, and `queued`. |
| `worker.ingest.transient` | warning | An envelope could not be stored because of a temporary error and was not acked. `error_code` is `ingest_transient`. Carries `type`, `seq`, `session_id`, and `error`. The API tries it again. |
| `worker.protocol.unknown_field` | warning | A receiver ignored fields it does not know. Carries `model`, `fields`, and `side` (`api` or `worker`). Rate limited per model. |
| `worker.protocol.unknown_type`, `worker.protocol.unknown_op` | warning | A message type or command op this side does not know. It was not acked as done. Carries `peer_name` and `side`. Rate limited. |
| `worker.session.stop_timeout` | warning | The worker did not report the durable `session.stopped` within 15 seconds of a stop, and the API dropped the lease anyway. |
| `worker.outbox.feature_missing` | warning | The worker did not send an envelope because the API did not list the feature its type needs. Carries `type` and `feature`. |
| `worker.handler.slow` | warning | The API took more than 1 second to handle one worker message. Carries `type` and `seconds`. Messages in the same lane waited (control messages, ingest, and deltas run in separate lanes). Rate limited per message type. |
| `worker.outbox.high`, `worker.outbox.full` | warning | The worker outbox is above 80 percent of its message or byte bound, or an append failed because it is full (`scope` is `worker` or `session`, one session may use half of the bound). The API is not acking fast enough or is down. Rate limited. |
| `worker.waiter.timeout` | warning | The worker waited too long for a presign or search reply. Carries `kind`. Rate limited. |
| `worker.message.invalid` | warning | The worker received a frame it could not parse or validate and skipped it. Carries `reason` (`malformed` or `invalid`) and, for a valid JSON object, `type`. Rate limited. |
| `worker.delta.oversize`, `worker.delta.rate_limited`, `worker.delta.rejected` | warning | The API dropped a live delta. Rate limited, so a line stands for `count` drops. |
| `background.loop.error` | warning | A background loop caught an error and went on. Carries `loop` and `error`. Alert on `apipi_background_loop_errors_total` as well. Rate limited per loop. |
| `background.task.failed` | error | A background task ended with an exception and no longer runs. Restart the process. |
| `event_bus.notify.failed` | warning | A Postgres `NOTIFY` publish failed. SSE on other replicas falls back to polling. Rate limited. |
| `worker.write.timeout` | warning | The API could not write a frame to a worker within 10 seconds, or the writer queue was full, and closes the connection with the reason `write_timeout`. Carries `why`, `timeout_seconds`, and `queued`. Rate limited. |
| `worker.message.skipped` | warning | The API skipped a message of a known type whose fields are not valid. Carries `type`. Rate limited per type. |
| `worker.frame.invalid` | warning | The API skipped a frame that is binary, not JSON, or not a JSON object. Carries `why`. Rate limited. |
| `worker.heartbeat.field_ignored` | warning | A heartbeat had an invalid optional field. The field was ignored and the leases were still extended. Carries `fields`. Rate limited. |
| `worker.connection.superseded` | warning | A heartbeat came from a connection whose generation is older than the worker's current one. The API closes it with the reason `takeover`. |
| `worker.lane.backpressure`, `worker.delta.queue_full` | warning | A lane of one socket is full. The ingest lane makes the socket wait. The delta lane drops deltas. Rate limited. |
| `worker.lease.revoke_failed` | warning | The reaper could not send `lease.revoke` (timeout or closed socket). The lease is already cleared. Rate limited. |
| `worker.connection.failed` | error | A task of one worker connection ended with an unexpected exception. The socket closes with the reason `error`. |
| `worker.detach.failed` | warning | The API could not clear `api_instance_id` after a disconnect. The next heartbeat or register sets it again. |
| `worker.message` | debug | One socket message, with `type` and `size`. Only with `APIPI_LOG_LEVEL=debug`. |
| `usage.export.dropped` | warning | Usage HTTPS export or sink dropped the event. |
| `payload.export.dropped` | warning | Payload HTTPS export or sink dropped the event. |

Cancelled turns stay `info` with `event` `turn` and code `cancelled`.
Completed turns use the same info line. HTTP request lines stay `info`
and include `error_code` when the response is an ApiPi error. Ship
stderr with a log collector; ApiPi does not bundle Grafana or Loki.
Alert on `failure_source=internal` and on upstream `5xx`, timeouts,
and connection errors. Do not page on each `upstream_rate_limited`.

## Query

Tenant-scoped. Wrong tenant is `404`. Tokens, turn counts, and search
counters, not USD. The numbers come from whatever hot data the store
still has.

| Method | Path |
| --- | --- |
| `GET` | `/v1/apipi/usage` |

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
  "turns": 0,
  "search_calls": 0,
  "search_units": 0
}
```

`turns` is the number of turn log rows still in Postgres, or the
rollup count for `day`. `search_calls` and `search_units` are the sums
of the same rows (see [Search](#search)). Missing counts are `0`. No
USD. No message text.

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

## Session lifecycle export

Per-turn usage export answers how many tokens a turn used. It does not
answer how long a session's sandbox was alive, or how many sandboxes a
tenant held at a given minute. Session lifecycle export is that
signal. The API owns it: `apipi serve` emits it. The worker only reports
over the worker socket: session live start and stop travel as durable
v2 envelopes, so they are buffered in the worker outbox during a
disconnect and replayed exactly once after reconnect, and the
periodic inventory live set is what the API derives heartbeats from.
`apipi worker` ignores `APIPI_LIFECYCLE_*` settings with a startup
warning, because it holds no export URL or token.

The feature is off unless `APIPI_LIFECYCLE_EXPORT_URL` or
`APIPI_LIFECYCLE_SINKS` is set. When it is off, the pool does not build
events and does not start a sender task. Hooks never wait on the
network. They enqueue a dict and return.

An image has no tag. Consumers identify a guest image by `sandbox_image`
(the image id), `image_version`, and `image_digest`. The version is the
computed `<pi_version>-<hash>` recorded in `<id>/current` at spawn. The
digest is `rootfs.sha256` from that version's `manifest.json`. A dev
rootfs override reports null for both version and digest.
`none` and non-microvm run modes send null for all three image fields.
`sandbox_size` is still set, because it drives the memory budget in
every run mode. `type=none` sessions are `environment_type: "none"`.
Split sandbox time from text-only concurrency with those two fields.

The version and digest are captured when the process is spawned and
stored on that live interval. A later `apipi images pull` that flips
`<id>/current` does not change an already-live session. The next
respawn captures the new version.

### Events

`session.live.start` is emitted once when a session goes from not live
to live. Reusing an already-live process for the next turn is not a
new start. A respawn after idle reap, or after a config change, is a
new start. `cause` is `spawn` or `respawn`.

`session.live.stop` is emitted once per start, just before the pool
runs its kill hook. `live_ms` is the duration from the monotonic clock
taken at spawn, so an NTP step does not change the billed duration.
`start_seq` is the `seq` of the matching start.

| `reason` | When |
| --- | --- |
| `idle` | Idle TTL expired |
| `stop` | Session stop or delete |
| `respawn` | Config change killed the process; a new start follows |
| `memory` | Host Pi exceeded `APIPI_PI_MEM_MIB` |
| `crash` | The process exited by itself |
| `drain` | Worker drain killed sessions that were not in a turn |
| `shutdown` | The pool owner is exiting |

`session.live.heartbeat` is one event per API replica per interval,
covering the sessions that replica's workers report as live in
their inventory, including when that set is empty. Set
`APIPI_LIFECYCLE_HEARTBEAT` to `0` or `off` to disable heartbeats.
Start and stop events still export. Each heartbeat entry repeats the
spawn-time fields: `session_id`, `start_seq`, `started_at`,
`tenant_id`, `org_id`, `agent_id`, `user_id`, `key_id`,
`environment_type`, `sandbox_image`, `image_version`, `image_digest`,
`sandbox_size`, and `run_mode`. The envelope carries `worker_id`,
`instance_id`, and `boot_id`. Entries do not repeat those, and they do
not include `reason`, `live_ms`, or `cause`.

There is no pre-warmed sandbox pool. A process is spawned only inside
`get(session_id)` during a turn, so it is bound to a session from
birth. A sandbox that is not bound to a session must not emit `start`
and must not appear in a heartbeat. If pre-warming is added later,
`start` is emitted when the sandbox is bound to a session, not when it
boots.

### Envelope

Every event has `schema_version` `1`, `type`, `event_id`, `boot_id`,
`seq`, and `ts`. `boot_id` is a new UUID for each process start.
`seq` starts at 1 and increases by one for each lifecycle event on that
`boot_id`, including heartbeats. `event_id` is `{boot_id}:{seq}` and
is the idempotency key. Order events for one `boot_id` by `seq`, not
by `ts`. `ts` is the worker's UTC wall clock and is approximate across
hosts. Workers should run NTP.

`worker_id` is the id from the hub `hello` message. `instance_id` is `APIPI_INSTANCE_ID`, the same value
usage events use. `org_id` is optional. The auth callback may return
it. ApiPi stores it on the session and forwards it to the worker. It
is null when the callback does not set it.

`user_id` defaults to the raw auth value, the same as usage export.
`APIPI_LIFECYCLE_USER_ID=omit` drops it. `hash` sends HMAC-SHA256 hex
of the raw id, keyed by `APIPI_LIFECYCLE_USER_ID_KEY` (process
environment only). The sink still receives tenant identifiers.

`APIPI_LIFECYCLE_RUN_MODES` is a comma-separated allow list. Empty
means every run mode. A process whose run mode is not listed builds no
lifecycle events.

### Reconciliation

Consumers should apply these rules. A reference implementation lives
in `apipi.services.lifecycle_export.reconcile`.

1. A `start` opens a live interval keyed by `(boot_id, start seq)`.
2. A `stop` closes it. `live_ms` is the duration. The interval end is
   the stop event's `ts`.
3. If a session with an open interval is missing from a later heartbeat
   of the same `boot_id`, close it at the last heartbeat `ts` that
   still listed it, with reason `lost`.
4. If no heartbeat arrives for a `boot_id` for `2 × interval_s` plus a
   grace you choose, close every open interval on that `boot_id` at the
   last heartbeat `ts`, with reason `worker_lost`.
5. A new `boot_id` for the same `worker_id` or `instance_id` means the
   old process is gone. Close its open intervals at its last heartbeat
   `ts`, with reason `worker_lost`. That also covers orphans swept
   after a crash. The new process does not emit stops for sessions it
   did not start.
6. Deduplicate by `event_id`. Order within a `boot_id` by `seq`.
7. If a heartbeat lists a session whose `start` was never received,
   open the interval from the entry so that usage is still attributed.

The worst over-count after a crash is one heartbeat interval.

### Delivery

The HTTP sender posts `{"events": [...]}` with at most
`APIPI_LIFECYCLE_BATCH` events, or sooner after
`APIPI_LIFECYCLE_BATCH_WAIT`. It retries 5xx, 429, 408, and network
errors with exponential backoff and jitter, up to
`APIPI_LIFECYCLE_RETRY_MAX`, and it does not skip the head batch.
Other 4xx responses drop the batch, log `lifecycle.export.dropped`,
and count `drop`. Custom sinks implement `emit(event)` and are called
from the sender task, not from the spawn hook. A failing sink is
logged and does not affect the others.

Delivery is at-least-once while the process lives. Retries can
duplicate events; dedupe on `event_id`. There is no durable outbox.
Events still queued when the process dies are lost. Heartbeat rules
above cover that gap. On shutdown the pool emits `shutdown` stops,
then flushes the queue for at most `APIPI_LIFECYCLE_EXPORT_TIMEOUT`.

When the queue is full, the new event is dropped.
`apipi_lifecycle_export_total{result="overflow"}` increments, and a
rate-limited warning is logged with `type` and `session_id`.
`apipi_lifecycle_queue_depth` is the current depth. Size
`APIPI_LIFECYCLE_QUEUE` for the burst you can tolerate losing. A full
queue drops the newest event, so a long outage loses the tail, not the
head that is already retrying.

Prometheus sandbox series stay aggregate and low-cardinality. Lifecycle
events are per session, pushed, and joinable, for metering. Both are
fed from the same pool hooks. `apipi_pi_kill_total` now includes
`crash` and `drain`. Worker drain used to increment `idle`.

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
| `apipi_workers` | gauge | connected sandbox workers (`run_mode`) |
| `apipi_worker_leases` | gauge | active session leases (`run_mode`) |
| `apipi_worker_assign_seconds` | histogram | time to assign a lease |
| `apipi_worker_heartbeat_gap_seconds` | histogram | seconds between consecutive worker heartbeats, on the API (receipt) and on the worker (send) |
| `apipi_worker_lease_events_total` | counter | `event` (`renewed`, `released`, `expired`) |
| `apipi_worker_ingest_total` | counter | `type` (envelope type), `result` (`ok`, `duplicate`, `rejected`) |
| `apipi_worker_ingest_rejected_total` | counter | `reason` (for example `not_leased`, `turn_mismatch`) |
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
| `apipi_pi_processes` | gauge | live host Pi processes |
| `apipi_pi_rss_bytes` | gauge | sum of host Pi process-group RSS |
| `apipi_pi_pss_bytes` | gauge | sum of host Pi process-group PSS |
| `apipi_pi_spawn_total` | counter | `result` (`ok` or `error`) |
| `apipi_pi_kill_total` | counter | `reason` (`idle`, `session`, `respawn`, `shutdown`, `memory`, `crash`, `drain`) |

`tenant` is the tenant id. Empty when the request has no tenant.
`path` is the route template, not the raw URL. `kind` is `prompt`,
`completion`, `cache_read`, `cache_write`, or `total`. Turn `status`
is `completed`, `failed`, or `cancelled`. Never prompt or completion
text.

Turn, token, and latency series are recorded once, on the process that
completes the turn, which is the worker. Set
`APIPI_METRICS` on the worker so those series are recorded there, and
scrape the worker at `http://<worker>:9091/metrics` (or
`APIPI_WORKER_METRICS_PORT`). The API process still has HTTP request
series and worker-pool gauges (`apipi_workers`, `apipi_worker_leases`,
`apipi_worker_assign_seconds`). It does not double-count turns.

Guest and host-Pi series stay low-cardinality (`size` is `S` / `M` /
`L` on guest series). They never use `session_id` or `user_id` as
labels. Host Pi series have no `size` label.

| Set | When | Series |
| --- | --- | --- |
| Worker util | All run modes | `apipi_worker_{capacity,sessions,memory_mib_*}` |
| Sandbox lifecycle | Any `PiPool` spawn | `apipi_sandbox_*` |
| Host Pi | `none` | `apipi_pi_*` (RSS/PSS of the Pi process group) |
| MicroVM guest | jailer `vm_id` | `apipi_guest_*` |

| Layer | What | Default | How |
| --- | --- | --- | --- |
| Host Pi RSS | Actual RAM of host Pi and its process group | On when worker metrics are on | `/proc/<pid>/smaps_rollup` (PSS) or `statm` (RSS only) |
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
Responses still echo `X-Trace-Id`. The API emits `session` and
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
