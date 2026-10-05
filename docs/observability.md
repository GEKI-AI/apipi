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

Production is `apipi serve` plus `apipi worker`, so scrape both and
set OTEL on both. Turn series and turn/model spans are recorded on the
worker. HTTP series and `worker.assign` spans are on the API. Set
`APIPI_METRICS` and `APIPI_OTEL_ENDPOINT` on both.

## Logs

Ship stderr. There is no log file in the gateway. Each line is one
JSON object with `timestamp`, `level`, `logger`, `message`, and
`service` (`apipi`). Error and warning lines that operators should
alert on also set `event` and `error_code`, plus `request_id`,
`tenant_id`, `session_id`, `turn_id`, and `worker_id` when known.

Every line a worker connection writes also carries `worker_id` and
`connection_id`. The API creates the `connection_id` when it accepts a
`register` and sends it to the worker in `hello.reply`, so you can join
the API and worker lines of one socket with it. Lines also carry
`lease_id`, `command_id`, `op`, and `request_id` where they apply, and
commands carry `traceparent`.

Normal traffic stays quiet. Info lines record one state change each:
connect, hello, disconnect, reconnect, drain, and lease changes.
Per-message lines are written only with `APIPI_LOG_LEVEL=debug`
(`event=worker.message`). Warnings that can repeat, such as a late
heartbeat or a rejected envelope, are written for the first
occurrence and then at most once per minute per event and per
connection (or per process for process-wide events). The later lines
carry `count`, the number of occurrences since the last line. Logs never
carry the command context, model or MCP keys, vault headers, presigned
URLs, search queries, or event payloads.

A microVM worker also writes one info line per guest connection
through the egress gateway (`event=egress.connection`). It carries
`session_id`, `host` (the server name or `Host` header, when the guest
sent one), `port`, `decision` (`spliced`, `intercepted`, or
`rejected`), `reason` when the connection was rejected or ended early
(for example `not_allowed`, `ip_literal`, `no_host`, `bad_host`,
`private_address`, `port`, `upstream_tls`, `host_mismatch`,
`bad_target`, `connect_method`, or `ambiguous_length`), and `bytes_up`
and `bytes_down`. It never carries header values, paths, or bodies.
When the worker runs out of file descriptors, the gateway stops
accepting for half a second and logs `egress.accept.failed` (warning,
at most once per minute).

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
`turn.failed` when `failure_source` is `internal`, and when the code
is `upstream_5xx`, `upstream_timeout`, `upstream_connection`, or
`upstream_error`. A host `429` (`upstream_rate_limited`) is warning:
alert on the rate, not on each line. Also alert on `api.error`,
`sandbox.boot.failed`, `worker.assign.failed`,
`worker.lease.expired`, and export drops. Codes are listed in
[failure codes](errors.md).

## Prometheus

No bearer. Network-restrict `/metrics` like any scrape endpoint.

| Process | Scrape | What you get |
| --- | --- | --- |
| API (`apipi serve`) | `http://<api>:8000/metrics` | HTTP requests, errors, `apipi_workers` and `apipi_worker_leases` (labeled `run_mode`), `apipi_worker_assign_seconds`, worker lease health and ingest series, socket, command, presign, and search series (see [Worker leases and ingest](#worker-leases-and-ingest) and [Worker socket metrics](#worker-socket-metrics)) |
| Worker | `http://<worker>:9091/metrics` | Turns, tokens, utilization, sandbox boot/destroy, host Pi RSS/PSS, cgroup guest RAM/CPU, optional vsock samples, `apipi_worker_heartbeat_gap_seconds`, connection, outbox, and command series (see [Worker metrics](#worker-metrics)) |

Worker metric sets (same scrape, metrics on):

| Set | When | Series |
| --- | --- | --- |
| Worker util | All run modes | `apipi_worker_{capacity,sessions,memory_mib_*}` |
| Event bus | Postgres store | `apipi_pg_notification_queue_usage`, `apipi_event_bus_listener_reconnects_total`, `apipi_event_bus_wake_sse_seconds` |
| Sandbox lifecycle | Any spawn through `PiPool` | `apipi_sandbox_*` |
| Host Pi | `none` (no `vm_id`) | `apipi_pi_processes`, `apipi_pi_rss_bytes`, `apipi_pi_pss_bytes`, `apipi_pi_spawn_total`, `apipi_pi_kill_total` |
| MicroVM guest | `vm_id` set | `apipi_guest_*` |
| Egress gateway | `microvm` | `apipi_egress_connections_total`, `apipi_egress_bytes_total` (see [Worker metrics](#worker-metrics)) |

`apipi_worker_memory_mib_used` is reserved guest budget for placement. `apipi_pi_rss_bytes` is actual host Pi RAM (process group, including MCP children Pi started). Guest jailer cgroup is `apipi_guest_memory_bytes`. Do not mix them.

`apipi_event_bus_listener_reconnects_total` counts Postgres `LISTEN` reconnects per replica; a steady climb means the database connection is flapping, and the fallback poll is covering the gaps. `apipi_event_bus_wake_sse_seconds` is the wake-to-SSE delivery latency after the storing read. `apipi_pg_notification_queue_usage` is the Postgres notification queue fill ratio from `pg_notification_queue_usage()`.

Guest resource layers:

| Layer | What | Default | How |
| --- | --- | --- | --- |
| A. Host / cgroup | Jailer cgroup memory and CPU | On when worker metrics are on | Read on the host. No guest code. |
| B. Guest sample | MemAvailable, load, workspace disk | Off | Tiny JSON over vsock. Set `APIPI_GUEST_SAMPLE_INTERVAL`. |
| C. In-guest Prometheus | node_exporter on TAP | Out of scope | Not lightweight. Attack surface. |

## Worker leases and ingest

A worker lease only stays alive while the API sees the worker. The
worker sends a heartbeat on its own timer, every `heartbeat_seconds`
from `hello.reply` (a third of `APIPI_WORKER_LEASE_TTL`, at most 10
seconds), whether or not the socket is busy. The API also renews the
leases of a worker that acks a command or has a durable batch
committed, at most once per heartbeat interval. These series show
whether that works:

| Series | Where | What it tells you |
| --- | --- | --- |
| `apipi_worker_heartbeat_gap_seconds` | API and worker | Histogram of the time between two heartbeats. On the API it is measured on receipt, so it includes the network. On the worker it is measured on the sending timer, so a high value there means the worker process was stalled. The gap should sit near the heartbeat interval. A gap above half the lease TTL also logs `worker.heartbeat.late`. |
| `apipi_worker_lease_events_total{event}` | API | `granted` counts leases handed to a worker with a command. `renewed` counts renewal passes (a heartbeat, an activity renewal, or a reconnect), not leases. `released` counts leases cleared by a release or a session stop. `expired` counts leases the reaper cleared. `orphaned` counts leases the worker did not report in an inventory. `revoked` counts leases the API told a worker to drop (the reaper, or an inventory or `hello` the lease does not match). `taken_over` counts leases that moved from an older socket of the same worker to a new one. |
| `apipi_worker_ingest_total{type,result}` | API | Durable envelopes by type. `result` is `applied`, `duplicate`, `rejected`, or `transient_error`. `transient_error` is an envelope the API could not store because of a temporary error (a deadlock, a lock or statement timeout, a connection reset, or a temporary object-store error such as a timeout or throttling, also on `artifact.completed`). The API did not ack it or anything after it in that session. It tries the batch again up to three times, and then closes the socket so the worker replays. Duplicates are normal after a reconnect replay and when the worker resends envelopes whose ack is still in flight. A steady stream of duplicates with no reconnect means a worker is sending sequence numbers the API already holds. |
| `apipi_worker_ingest_rejected_total{reason}` | API | Rejected envelopes by reason. `not_leased` means the worker sent results for a session it no longer holds, so those results were dropped. |

Every lease expiry logs `worker.lease.expired` with
`last_renewal_age_seconds` and, when the worker is still connected,
`last_heartbeat_age_seconds`. Compare them with the lease TTL to tell a
dead worker from a stalled one.

`apipi_worker_protocol_total{event}` stays. It counts delta and
rejected-envelope outcomes (`delta.accepted`,
`delta.rejected`, `delta.dropped_done`, `delta.rate_limited`,
`delta.oversize`, `delta.invalid`, `delta.reasoning_dropped`, and
`envelope_rejected`) and the API paths that skip, retry, or drop work
on a worker socket: `frame_invalid` (a binary or non-JSON
frame), `message_invalid` (a known message type with invalid fields),
`message_failed` (a handler raised and the socket stayed open),
`message_retried`, `heartbeat.field_ignored`, `delta.queue_full`,
`lane.backpressure` (a full lane made the socket wait), `ingest.retried`,
`ingest.failed` (the batch failed after retries and the socket closed), `unknown_field` (a field the receiver does not know was ignored; it counts each field of a message), `unknown_type` (a message type this side does not know), `unknown_op` (a command op the worker does not know), `presign.rereplied` (a replayed `artifact.presign` got its reply again), `stop_timeout` (`session.stopped` did not arrive in 15 seconds),
`superseded` (a heartbeat from an older generation), and
`revoke_failed` (the reaper could not send `lease.revoke`). The register outcomes moved to
`apipi_worker_connects_total{result}` and the message counts to
`apipi_worker_messages_total`.

Prometheus labels stay low-cardinality. `tenant` is allowed. Do not
put `session_id` or `user_id` on series. Scrape node_exporter on the
worker host if you need machine disk and NIC.

## Worker socket metrics

These series are exposed by the API (`apipi serve`). Each replica counts
its own sockets, so sum over replicas. A series that this page says stays
at zero is defined, and it is filled by a later change to that path.

| Series | Type and labels | What it tells you |
| --- | --- | --- |
| `apipi_worker_connections` | gauge, `run_mode` | Open worker sockets on this replica. It matches `apipi_workers`. |
| `apipi_worker_connects_total` | counter, `result` | Register outcomes. `ok`, or the reject reason: `unauthorized`, `revoked`, `invalid_register`, `unsupported_protocol`, `token_bound`, `register_timeout`, `closed`. |
| `apipi_worker_disconnects_total` | counter, `reason` | Why a socket closed: `clean`, `error`, `ping_timeout` (the server's keepalive got no answer), `write_timeout` (a frame was not written in 10 seconds or the writer queue was full), `takeover` (the same worker connected again, or a heartbeat came from an older generation), `revoked`, `protocol_violation`, `ingest_failed`. See [what closes a connection](workers.md#what-closes-a-connection-and-what-does-not). |
| `apipi_worker_messages_total`, `apipi_worker_message_bytes` | counter and histogram, `direction` (`in` or `out`), `type` | Messages and frame sizes by the fixed message or envelope type. Anything else is `unknown`. |
| `apipi_worker_handle_seconds` | histogram, `type` | Time to handle one message in its own lane: the control lane for `heartbeat`, `lease.ack`, `lease.release`, `inventory`, and `sandbox.seen`, the ingest lane for envelopes (measured per batch, so every envelope type in a batch gets the batch time), and the delta lane for deltas. A slow `type` only delays the messages in its own lane. A handler over 1 second also logs `worker.handler.slow`. |
| `apipi_worker_ingest_batch_seconds`, `apipi_worker_ingest_batch_size` | histograms | Batch commit time and the number of envelopes per batch. |
| `apipi_worker_commands_total` | counter, `op`, `result` | Command delivery: `sent`, `acked`, `retransmitted` (resent after a reconnect, or on the 5 second timer), `timeout` (no `lease.ack` within the wait of the caller), `expired` (no `lease.ack` within the lease TTL, so the lease was cleared and the turn failed with `worker_command_timeout`), `failed` (the send raised). |
| `apipi_worker_command_ack_seconds` | histogram, `op` | Time from sending a command to its `lease.ack`. |
| `apipi_worker_commands_unacked` | gauge | Commands waiting for `lease.ack`. |
| `apipi_worker_forwards_total` | counter, `op`, `result` | Commands this replica forwarded to the replica that holds the worker socket. `result` is `ok` (the wait the caller asked for was reached), `failed` (an error came back), `timeout` (the other replica did not answer in time), or `queued` (the reaper's revoke, which does not wait). See [commands across API replicas](workers.md#commands-across-api-replicas). |
| `apipi_worker_forward_seconds` | histogram, `op` | Time from storing a forward to its result, as the requesting replica sees it. A `session.stop` includes the wait for `session.stopped`. |
| `apipi_worker_forward_failures_total` | counter, `reason` | Why a forward failed: `replica_stale` (the replica stopped heartbeating, failed at once), `not_connected` (the worker left that replica), `timeout`, `ack_timeout`, `rejected` (the other replica refused: capacity, image, size), or `error`. |
| `apipi_worker_send_queue_depth` | gauge | Frames waiting in the per-connection writers of this replica, summed over all sockets. It should stay near zero. A value that stays high means a worker reads slower than the API sends. At 1024 frames on one socket, or 10 seconds on one frame, that socket closes with `write_timeout`. |
| `apipi_worker_presign_total`, `apipi_worker_presign_seconds` | counter and histogram, `kind` (`artifact`, `pi_session`, `input_image`), `result` | Artifact presign outcomes (`ok`, `unchanged`, a quota code, or `store_error`) and handling time. |
| `apipi_search_requests_total`, `apipi_search_seconds`, `apipi_search_inflight` | counter, histogram, gauge, `provider`, `result` | `web_search` outcomes (`ok`, the provider error code, or `search_denied`), provider call latency, and running calls. |
| `apipi_event_bus_notify_errors_total` | counter | Failed `NOTIFY` publishes, whatever the error (a closed connection, a timeout, or an unexpected exception). Publishes are serialized on one connection, which is dropped and reopened after an error. The fallback poll covers the gap, but SSE clients on other replicas are slower. |
| `apipi_background_loop_errors_total`, `apipi_background_loop_last_run_timestamp` | counter and gauge, `loop` | Errors a background loop caught and the Unix time of its last finished round. API loops: `lease_reaper`, `delta_flusher`, `usage_purge`, `attachment_sweep`, plus the tasks `lifecycle_heartbeat` and `lifecycle_sender`. |
| `apipi_event_loop_lag_seconds` | histogram | How late the event loop wakes a one second sleep. High values mean something blocks the loop. |
| `apipi_worker_info` | gauge, `worker_id`, `protocol`, `version`, `run_mode` | Always 1, one series per connected worker. Use it for fleet version views. This is the only series with `worker_id`. |

## Worker metrics

These series are exposed by the worker (`apipi worker`) on its
`/metrics` port.

| Series | Type and labels | What it tells you |
| --- | --- | --- |
| `apipi_worker_connected` | gauge | 1 while the socket is up and `hello` was received. |
| `apipi_worker_reconnects_total` | counter, `reason` | Reconnect attempts: `closed`, `connect_error`, `ping_timeout` (no pong within 10 seconds), `hello_timeout` (no `hello.reply` within 15 seconds), `error`. |
| `apipi_worker_connect_seconds` | histogram | Dial plus handshake time. |
| `apipi_worker_outbox_messages`, `apipi_worker_outbox_bytes` | gauges | Unacked envelopes and their size, sampled every second. |
| `apipi_worker_outbox_oldest_seconds` | gauge | Age of the oldest unacked envelope. This is the main alert for a stuck API or socket. |
| `apipi_worker_outbox_full_total` | counter | Turns failed with `worker_outbox_full`. An envelope over `MAX_MESSAGE_BYTES` fails the turn with `worker_message_too_large` and logs `worker.outbox.oversize`; it has no counter of its own. |
| `apipi_worker_ack_seconds` | histogram | Time from outbox append to the cumulative ack. |
| `apipi_worker_replayed_total` | counter | Envelopes resent after a reconnect or a restart (envelopes that were sent before and are still unacked). First sends are not counted. |
| `apipi_worker_spool_write_seconds`, `apipi_worker_spool_bytes` | histogram, gauge | Disk spool append and compaction cost and spool size, only with `APIPI_WORKER_OUTBOX_DIR`. |
| `apipi_worker_commands_received_total` | counter, `op`, `result` | Command handling: `dispatched`, `duplicate` (a retransmit that was only acked), `rejected` (invalid or without a tenant), `failed`, `unknown_op` (an `op` the worker does not know, with `op="unknown"`; it is not acked). |
| `apipi_worker_command_seconds` | histogram, `op` | Dispatch time. For `turn.start` and `turn.continue` it includes the turn run. |
| `apipi_worker_waiter_total` | counter, `kind` (`presign`, `search`), `result` (`ok`, `timeout`, `disconnected`) | Waits for a reply from the API. `disconnected` is a wait that ended because the socket closed. A presign wait ends this way only when the API had acked the request, so its reply was lost; otherwise the worker keeps waiting for the replay. |
| `apipi_worker_deltas_dropped_total` | counter, `reason` | Live deltas the worker did not send: `disconnected` (no socket) or `oversize` (over the message limit). |
| `apipi_worker_draining` | gauge | 1 while the worker drains. |
| `apipi_egress_connections_total` | counter, `decision` | Guest connections through the microVM egress gateway: `spliced` (passed through unchanged), `intercepted` (the gateway read each HTTP request: TLS ended at the gateway, or plain HTTP on port 80 with `restricted`), or `rejected` (by policy, a private address, or a failed upstream connect). A rising `rejected` rate on one worker usually means a session tries hosts its policy does not allow. Each connection also logs `egress.connection`. |
| `apipi_egress_bytes_total` | counter, `direction` | Bytes through the egress gateway. `up` is guest to upstream, `down` is upstream to guest. On intercepted connections it counts the HTTP bytes after TLS. |
| `apipi_background_loop_errors_total`, `apipi_background_loop_last_run_timestamp`, `apipi_event_loop_lag_seconds` | as on the API | Worker loops: `worker_observe`, `session_reaper`, `workspace_reaper`, `sandbox_seen`, `outbox_metrics`, `outbox_spool`. Tasks: `worker_metrics`, `worker_command`, `worker_stop`, `worker_revoke`, `worker_release`. |

`apipi_worker_heartbeat_gap_seconds` is exposed by both processes, as
described above.

## Traces

When `APIPI_OTEL_ENDPOINT` is set, ApiPi exports OTLP/HTTP traces.
`/v1/traces` is appended if missing. Spans are wait-focused:
`session`, `worker.assign`, `sandbox.boot`, `sandbox.attach`,
`turn`, `model`. Inbound `traceparent` is honored. The worker
command carries it so the API and the worker stay on one trace.

Point the endpoint at your collector (Tempo, Jaeger, or a vendor).
Use traces to see where time went on a slow turn. Use Prometheus for
rates and saturation. Use logs for error codes.

## Usage export

`GET /v1/apipi/usage` is the hot store: tenant-scoped session, turn, or
day. Long-term “who used what” is `APIPI_USAGE_EXPORT_URL`: one JSON
event per turn, tokens and counts only, never USD. Join with
`tenant_id`, `user_id` (when the auth plugin set it), `agent_id`,
`session_id`, `turn_id`, and `request_id`.

Auth plugins may return `user_id`. ApiPi does not invent it from
`key_id`. `X-User-Id` on HTTP responses is still `key_id`. See
[auth](auth.md) and [usage](usage.md).

## Session lifecycle export

`APIPI_LIFECYCLE_EXPORT_URL` is the per-session live-phase feed:
sandbox start, stop, and a heartbeat of the live set. It is how a
host meters active sandbox time by environment, image, and size.
Prometheus stays aggregate. `apipi_sandboxes_active` and
`apipi_pi_kill_total` do not carry `session_id`. Join billing rows on
`event_id` (`{boot_id}:{seq}`), not on a scrape.

The API emits the events. Scrape `apipi_lifecycle_export_total`
and `apipi_lifecycle_queue_depth` on the API. `result` is `ok`, `retry`, `drop`, or
`overflow`. Size the queue for the exporter outage you can tolerate.
A full queue drops the newest event.

Run NTP on workers. `ts` is wall clock and is only for ordering across
hosts. `live_ms` comes from the monotonic clock and is the duration to
bill. See [usage](usage.md#session-lifecycle-export).

## Suggested alerts

| Signal | Why |
| --- | --- |
| Rate of `event=turn.failed` with `failure_source=internal`, or `apipi_turns_total{status="failed"}` | Turns dying inside ApiPi |
| `apipi_errors_total` 5xx / `event=api.error` | Gateway faults |
| HTTP `429` with `capacity` / `event=worker.assign.failed` | Node or tenant full |
| `apipi_sandbox_boot_total{result="error"}` / `event=sandbox.boot.failed` | Guests not starting |
| `apipi_worker_sessions` near `apipi_worker_capacity` | Packing too tight |
| `apipi_pi_rss_bytes` near host RAM on a `none` worker | Dense Pi packing |
| `apipi_worker_assign_seconds` p95 | Lease wait |
| `apipi_usage_export_total{result="drop"}` | Warehouse gaps |
| `apipi_lifecycle_export_total{result="overflow"}` | Lifecycle queue full; live intervals may be missing |
| `event=worker.lease.expired`, `apipi_worker_lease_events_total{event="expired"}` | Worker died or heartbeat failed |
| `event=worker.heartbeat.late`, or `apipi_worker_heartbeat_gap_seconds` p99 above half the lease TTL | A lease will expire soon: stalled worker or a bad network |
| `apipi_worker_ingest_rejected_total{reason="not_leased"}` | Worker results were dropped after a lease ended |
| `apipi_worker_outbox_oldest_seconds` above the lease TTL | A worker cannot get its results acked: API down, socket stuck, or ingest failing |
| Rate of `apipi_worker_lease_events_total{event="expired"}` | Leases die on a live fleet |
| Rate of `apipi_worker_disconnects_total` (not `clean`) and of `apipi_worker_reconnects_total` | Flapping sockets |
| `apipi_worker_heartbeat_gap_seconds` p99 | Alert when it nears half the lease TTL |
| `apipi_worker_handle_seconds` p99 by `type` | A slow lane. Alert on `heartbeat` and `lease.ack` first: their lane must stay fast. |
| `apipi_worker_send_queue_depth` above 0 for minutes, or `apipi_worker_disconnects_total{reason="write_timeout"}` | A worker that does not read its socket |
| Rate of `apipi_worker_protocol_total{event="message_failed"}` and `{event="ingest.failed"}` | Database trouble that the socket survived, or did not |
| `apipi_event_loop_lag_seconds` p99 | A blocked event loop on the API or a worker |
| `apipi_worker_connected` equal to 0 for longer than the lease TTL, or rate of `apipi_worker_reconnects_total` by `reason` (`ping_timeout`, `hello_timeout`) | A worker cannot reach the API, a half-open network path, or an API that accepts sockets and does not answer |
| `apipi_worker_waiter_total{result="timeout"}` or `result="disconnected"` | A presign or search reply never arrived; artifacts of a turn may be missing |
| `event=worker.outbox.oversize` and `event=worker.drain.finished` with `error_code=drain_timeout` | A turn failed with `worker_message_too_large`; a worker stopped with results or sessions left |
| `time() - apipi_background_loop_last_run_timestamp` for `lease_reaper` and `delta_flusher` | A dead or hung background loop. Also alert on `apipi_background_loop_errors_total`. |
| Rate of `apipi_worker_commands_total{result="timeout"}`, `result="retransmitted"`, and `result="expired"` | Workers that do not ack commands. `expired` means a lease was cleared and a turn failed. |
| `apipi_worker_protocol_total{event="unknown_field"}`, `{event="unknown_type"}`, `{event="unknown_op"}` | A peer runs another version. Fields are ignored, types and ops are not handled. |
| `apipi_worker_ingest_total{result="transient_error"}` | The API cannot store worker results |
| `apipi_search_requests_total{result!="ok"}` | Search provider errors or denials |
| `apipi_event_bus_notify_errors_total` | `NOTIFY` failures |
| Rate of `apipi_worker_forward_failures_total` by `reason` | `replica_stale` means a replica died with workers attached. `timeout` means a replica is slow or its listener is down. Also alert on a high `apipi_worker_forward_seconds` p99. |

## Cardinality

| Signal | Identity |
| --- | --- |
| Prometheus | `tenant` ok. Not `user_id`, `session_id`, `lease_id`, or `request_id`. Guest series use `size` (`S` / `M` / `L`). Worker series use the fixed message `type`, `op`, `reason`, or `result`. `worker_id` is only a label of `apipi_worker_info`. |
| Logs and traces | `request_id`, `tenant_id`, `session_id`, `turn_id`, `worker_id`, `connection_id`, `lease_id`, `command_id` when known |
| Usage export | `tenant_id`, `user_id`, `key_id`, `agent_id`, `session_id`, `turn_id`, `request_id` |
| Lifecycle export | Same identity as usage, plus image id, version, and digest. Not a Prometheus label. |
