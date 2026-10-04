# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- File kinds, session file bindings, and paginated file lists (#513). Every file has a `kind`: `file` (uploaded for agents and setup), `attachment` (a presigned upload with `purpose: "attachment"`, which is now stored instead of being an alias), or `image` (an image sent with a message), and the `user_id` of the uploader (images take the session's). For images uploaded before they are sent by `file_id`, `POST /v1/files` accepts the OpenAI purpose `vision` and `POST /v1/apipi/uploads` accepts `purpose: "image"`; both create a file of kind `image` and check the type against `APIPI_IMAGE_MIMES` and the size against `APIPI_MAX_IMAGE_BYTES`. Data URL images keep purpose `user_data`. The new table `session_files` binds a file to a session with an optional workspace `path` and the `item_id` of its user item, which is filled in when the worker stores that item. Images sent with a message, by data URL or by `file_id`, are bound to the session; old workers that still upload input images get the same. `GET /v1/files` is paginated (`limit` 1 to 100, default 20, `after`, `order`, `purpose`) and lists only kind `file` unless `include_attachments=true`. New routes: `GET /v1/apipi/files` (filters `kind`, `session_id`, `user_id`, `purpose`, `filename` prefix; objects add `kind`, `user_id`, `content_type`) and `GET /v1/apipi/sessions/{session_id}/files`. Deleting a session deletes the attachments and images no other session uses; deleting a file deletes its bindings. Unbound attachments older than `APIPI_ATTACHMENT_TTL` (default `24h`, must be greater than zero) are deleted by the hourly `attachment_sweep` loop. An attachment used in `environment.files` of a session or in an agent's `session_defaults` becomes kind `file`, so the sweep never deletes an agent input. Migration `0031_file_kinds` sets existing input images to kind `image` and binds them to their session.

### Changed

- Workspace restore writes only missing files (#514). Files from `environment.files` (Files API ids and inline content) are fetched and written before a turn only when their path does not exist in the session directory. Agent edits to `inputs/` are no longer overwritten on the next turn; they last until the workspace is rebuilt (TTL wipe in isolation `none`, guest stop in `microvm`), and then the original files come back. A turn on a running microvm guest fetches no file bytes.

### Fixed

- Images no longer travel as base64 in the worker command, so an image up to `APIPI_MAX_IMAGE_BYTES` no longer fails with `413` because the command is over 256 KiB (#512). The gateway stores each `input_image` data URL as a file before the turn, and `turn.start` carries image references (`file_id`, `object_id`, `url` or `local_path`, `mime_type`, `size_bytes`) in `parts`, which the worker fetches like the context files. The worker no longer uploads input images. This needs the new worker protocol feature `image_refs`. Placement prefers a worker that lists it, and a message with images that still lands on a worker without it fails with `501` `unsupported_op`, so upgrade the API first and then the workers (roll back the workers first). A forwarded turn stores only the image `file_id` in the forward row, and the owning replica signs the reference. Data URL images of a turn that does not start are deleted again. `input_image` also accepts `file_id` (a file of the tenant with an allowed image type within the image size limit) and ignores `detail`. A command that is still too large fails before the turn with a message that says so.

## [0.14.1] - 2026-10-03

### Fixed

- The Images workflow called `apipi images publish`, which #460 removed, so the `v0.14.0` run built the guest images and then failed before it could publish the store. It now uses `apipi images push`. `0.14.0` has no guest image assets on its GitHub release: upgrade to `0.14.1`, or on `0.14.0` set `APIPI_IMAGE_STORE_VERSION=0.14.1` and run `apipi images pull`. The Python package itself is unchanged.

## [0.14.0] - 2026-10-03

### Added

- Model credential callback (#503). `model_credential(identity, bearer)` returns the key Pi sends to the model host, so a turn on any API replica reaches it. Set it with `APIPI_MODEL_CREDENTIAL` (TOML `model_credential`) or `Gateway.create(model_credential=...)`; the type is `apipi.ModelCredential`. The order is `OPENAI_API_KEY_OVERWRITE`, then the callback, then the request bearer, and a request with none of them fails at once with `503` `model_key_unavailable`. A forwarded command resolves the key on the owning replica from the identity stored in its row (`key_id`, `user_id`, `org_id`, tenant); the raw bearer is still never stored or forwarded. The worker now applies the key of the command context to the credential broker at the start of every turn, so a short-lived or rotated credential works for a Pi that stays up. `worker.forward.model_key_dropped` is removed. `examples/model_credential.py` shows an HMAC-signed token and the model host check.

- Worker protocol specification (#489). New page `docs/worker-protocol.md` is the normative, language-neutral spec of worker protocol v2: transport, data types, every message with its fields, command ops and the command context, the events a worker must emit and their order, the state machines, sequencing and delivery, timers and limits, close codes and errors, versioning and features, security rules, and what a worker must implement. JSON Schema for every message is generated from the protocol models into `docs/worker-protocol/schema/` (`uv run python scripts/gen_worker_schema.py`; a test fails when the files are stale, and every frame the API and the worker send in the tests is validated against them). Golden transcripts of 14 scenarios are in `tests/fixtures/worker-protocol/*.jsonl`, and the real API and the real worker each play them in the tests, so a worker in another language can run the same transcripts. `docs/workers.md`, `docs/worker-concepts.md`, and ADR 0015 are corrected and link to the spec.
- Worker protocol monitoring (#485). New API series for the worker socket (`apipi_worker_connections`, `apipi_worker_connects_total`, `apipi_worker_disconnects_total`, `apipi_worker_messages_total`, `apipi_worker_message_bytes`, `apipi_worker_handle_seconds`, `apipi_worker_ingest_batch_seconds`, `apipi_worker_ingest_batch_size`, `apipi_worker_commands_total`, `apipi_worker_command_ack_seconds`, `apipi_worker_commands_unacked`, `apipi_worker_send_queue_depth`, `apipi_worker_presign_total`, `apipi_worker_presign_seconds`, `apipi_search_requests_total`, `apipi_search_seconds`, `apipi_search_inflight`, `apipi_event_bus_notify_errors_total`, `apipi_worker_info`) and for both processes (`apipi_background_loop_errors_total`, `apipi_background_loop_last_run_timestamp`, `apipi_event_loop_lag_seconds`), new worker series for the connection, outbox, commands, waiters, and drain, new lease events (`granted`, `orphaned`, `revoked`, `taken_over`), and the ingest result `applied` instead of `ok`. Register outcomes are now `apipi_worker_connects_total{result}`; `apipi_worker_protocol_total{event}` keeps the delta and rejected-envelope counters. `hello.reply` carries an optional `connection_id` and `register` an optional `version`. Worker log lines carry `worker_id` and `connection_id`, state changes are logged at info, and repeating warnings are rate limited with a `count`. See `docs/observability.md` and the event table in `docs/usage.md`.
- Built-in `web_search` tool (#478). An agent turns search on with `{"type": "web_search"}` in `tools`, with an optional `filters.allowed_domains` list (at most 10 domains). The operator configures one provider on the API with the new `[search]` table (`APIPI_SEARCH_PROVIDER` `tavily` or `staan`, `APIPI_SEARCH_API_KEY` from the environment only, `APIPI_SEARCH_BASE_URL`, `APIPI_SEARCH_TIMEOUT`, `APIPI_SEARCH_MAX_RESULTS`, `APIPI_SEARCH_TAVILY_DEPTH`, `APIPI_SEARCH_STAAN_MARKET`). A provider without a key fails at startup. Agent create or update with a `web_search` tool returns `400` with code `search_not_configured` when search is not configured. The Pi tool calls the session broker, the worker forwards the call to the API over the new synchronous `search.request` and `search.reply` messages, and the API calls the provider, so no provider name or key reaches a worker or a guest. It works on `type=none` and `microvm` sessions. Results use one normalized shape for every provider. A provider error, a timeout, or a lost worker socket is a tool error, not a turn failure. Each call is a `web_search_call` item (migration `0028`). Every search decision goes through one resolver, which prepares per-tenant search later. `search_not_configured` and the `search.denied` log event are new. `search_context_size`, `user_location`, and `web_search_preview` return `not_implemented`.
- Search usage: turn logs, daily rollups, `GET /v1/apipi/usage`, the `usage` event, and the usage export now carry `search_calls` and `search_units`, and the turn log and usage event also carry `search_counts` per provider and key source. Only calls the provider charged are counted. The Constitution Law 3, ADR 0006, and the `AGENTS.md` Do-not line now allow routing `web_search` to a configured provider. A search engine and a browser engine in the gateway stay forbidden. See `docs/tools.md`, `docs/usage.md`, and `docs/workers.md`.
- `apipi dev` starts the API and one worker for local development, as two child processes with combined log output. It runs `apipi migrate`, creates or reuses a worker token in `.apipi/dev-worker-token` (mode `0600`), runs both processes in the same directory so they share the default `APIPI_LOCAL_STORE_DIR` (`.apipi/store`), and starts `apipi serve` and `apipi worker` with `APIPI_WORKER_TOKEN_FILE` and `APIPI_API_URL` pointing at the local API. The worker run mode comes from `APIPI_RUN_MODE` (default `none`). Ctrl-C stops both processes, and if either one exits the other is stopped. Flags: `--config`, `--host` (default `127.0.0.1`), and `--port` (default `8000`).
- `APIPI_LOCAL_STORE_DIR` now defaults to `.apipi/store`, so a single-host setup with `APIPI_ARTIFACT_STORE=local` works without extra config. The local-store check applies to every `apipi serve`.
- Artifacts via API-issued presigned PUT with no object-store credentials on workers (#448). The split worker holds no blobs or objects and performs no artifact or file database writes: `LocalExecution` runs credential-less and `OutboxSink` uploads through `artifact.presign` (outbox) -> `artifact.presign.reply` (same socket) -> PUT (S3, plain HTTPS) or shared-root write (filesystem, to the reply `path`) -> `artifact.completed` (outbox), for `artifact`, `pi_session`, and `input_image` kinds, including input images in `run_turn` and the killed-process harvest. The killed harvest uses the tenant identity from the live sinks and the workspace directory remembered from the turn context, with no database access. The total workspace limit is enforced on the worker from settings in both split harvest paths, with the `workspace_too_large` code. The worker sends durable `artifact.presign` (session id, kind, filename, content type, size, checksum; never bytes); the API checks quotas (`max_workspace_bytes`, `max_artifact_bytes`, or `max_file_bytes` for input images) before reserving the slot and answers with `upload_id`, `url`/`headers`/`expires_at` (S3) or the exact store-root relative `path` (filesystem), plus `object_id` and `file_id` for input images. The API verifies the object (S3 `HEAD` size and checksum, or shared-root size and checksum), rejects a foreign `upload_id`, a path that does not match the reserved key, and checksum or size mismatches, then writes rows with the presigned `artifact_id` (artifacts), the file row with `file_id` (input images), or the session Pi pointer the turn context uses for cold restore (Pi sessions). When the presigned digest matches the latest stored bytes for that artifact path, the API answers `unchanged` instead of reserving a slot (before quota) and the worker skips the upload with no new row. Pi presigns reuse the existing blob id so saves overwrite one object. Quota failures return today's codes and fail the turn the way direct writes do. New `APIPI_LOCAL_STORE_DIR` is the dedicated local store root; turn-context references now use it. At register the API writes a nonce marker into the root and sends it in `hello.reply`; a worker that cannot read it back is rejected with `filesystem store requires a shared path`. New `artifact_uploads` ledger (migration `0027`). Production is `apipi serve` plus `apipi worker`. See `docs/workers.md`, `docs/scale.md`, `docs/production.md`, and `docs/config.md`.
- Live delta relay over the worker socket (#445): in split mode the
  worker coalesces model text fragments over about 40ms per session
  and sends them as ephemeral v2 `delta.text` envelopes instead of
  publishing them itself. The API replica holding the socket checks
  that the session is leased to that worker, applies a 32 KiB size
  cap and a per-session rate budget (100 deltas per second), drops
  deltas for turns that already committed `output_text.done` (the
  final item stays the source of truth), and publishes the rest as
  `live` messages on the event bus without any store write, so SSE
  token streaming works on any replica with no sticky routing.
  `delta.reasoning` envelopes are accepted but never fanned out.
  Outcomes are counted in `apipi_worker_protocol_total`. See
  `docs/workers.md` (Live deltas) and `docs/scale.md`.
- Event bus with Postgres `LISTEN`/`NOTIFY` fan-out for session
  events. After the API commits stored events it publishes a wake
  (`session_id`, `seq`); SSE streams wait for wakes instead of
  polling every 250ms, and `RemoteExecution._wait` waits on the same
  mechanism instead of polling every 50ms. A fallback poll
  (`APIPI_EVENT_BUS_FALLBACK_POLL`, default `3s`) covers lost
  notifications and listener reconnects, so an idle stream queries
  the store no more often than that interval. `APIPI_EVENT_BUS` is
  `auto` (Postgres on a Postgres store, in-memory on SQLite),
  `memory`, or `postgres`; explicit `postgres` on SQLite fails at
  startup. Each Postgres replica holds one dedicated `LISTEN`
  connection outside the SQLAlchemy pool, and each payload carries
  its sender so a replica never re-delivers its own broadcast.
  Until durable worker ingest (#446) the worker still writes events
  itself, so `apipi worker` builds the same bus from its own
  `DATABASE_URL` and must point at the same database as the API (turn
  context still comes from that database until #447). Live `output_text.delta`
  batches are coalesced over about 40ms and stay under the
  8000-byte `NOTIFY` limit (larger batches are split); deltas are
  never stored. New metrics: `apipi_event_bus_listener_reconnects_total`,
  `apipi_pg_notification_queue_usage`, and
  `apipi_event_bus_wake_sse_seconds`. SSE no longer needs sticky
  routing on a Postgres store (see `docs/scale.md`).
- Worker commands (`turn.start`, `turn.continue`, `sandbox.boot`) carry a
  `context` object built by the API: session environment and identity,
  the resolved agent definition, the effective idle TTL, the HTTP MCP
  servers with vault headers applied, the model key, and references
  (never bytes) to workspace files, skills, and the Pi session blob.
  The worker prepares the turn from the context instead of reading the
  session, agent, file, and skill rows, so HTTP MCP tools work in split
  mode on first and follow-up turns, including follow-ups on another API
  replica. With `APIPI_ARTIFACT_STORE=s3` references are presigned GET
  URLs; with the filesystem store they are paths relative to the shared
  store root. Contexts are validated (bytes rejected, 256 KiB command
  cap) and never logged. The workspace reaper uses the context TTL with
  no database read when the worker knows the session.

- Agent option to disable built-in tools: `metadata["apipi.builtin_tools"]`
  (`on`, `off`, default `on`) on the agent or session, with the session
  value winning over the agent. `off` starts Pi with
  `--no-builtin-tools` when MCP or function tools are present and
  `--no-tools` otherwise, plus `--no-skills`, so skills are not loaded
  and no `capability.json` is written. The guest still boots and still
  collects `outputs/` artifacts; the platform prompt uses the `none`
  fragment. An invalid value is `400` (`invalid_request`). The key is
  portable in agent bundles, and `apipi.codemode` is portable now too.
- Durable worker events ingested by the API (#446). The turn runtime
  reports turns, items, events, usage, and errors through a
  `ResultSink` interface instead of writing to the store directly.
  Combined `apipi serve` uses a direct-DB sink, so its behaviour is
  unchanged. A split-mode worker buffers durable v2 envelopes
  (`item.added`, `item.done`, `turn.status`, `usage`, `event`,
  `session.status`, `error`) in a bounded per-session outbox until
  the cumulative `ack{last_seq}`, with an optional disk spool
  (`APIPI_WORKER_OUTBOX_DIR`, capped by
  `APIPI_WORKER_OUTBOX_MAX_MESSAGES` and
  `APIPI_WORKER_OUTBOX_MAX_BYTES`). The API ingests each
  connection's envelopes in batches (about 50ms via
  `APIPI_WORKER_INGEST_BATCH_WINDOW`, or
  `APIPI_WORKER_INGEST_BATCH_SIZE` messages), one transaction per
  batch: a `worker_ingest` ledger (`UNIQUE(session_id, worker_seq)`)
  makes replays idempotent, the public `events.seq` stays
  API-assigned, and `sessions.worker_seq` (migration `0026`)
  records the ack cursor that `hello.reply` reports, so a reconnect
  to any replica replays exactly what is missing. Ingest validates
  the lease, the turn binding, and size limits; violations drop the
  message, log `worker.event.rejected`, and count in
  `apipi_worker_protocol_total{event="envelope_rejected"}`.
  `artifact.completed` is now applied by #448; `sandbox.status` stays rejected until
  #449 applies it. After commit the API sends the ack, then
  publishes the `EventBus` wake. The worker keeps the turn running
  across a dropped socket or an API restart within the lease TTL;
  a full outbox fails the turn with `worker_outbox_full`. Tool and
  MCP tallies for the turn log are collected in memory while the
  turn runs. See `docs/workers.md`, `docs/worker-concepts.md`, and
  `docs/config.md`.
- Lifecycle, sandbox status, reaper, and wipe as events, inventory
  reconcile, and API-owned lease takeover (#449). The worker makes
  no database queries. Sandbox transitions travel as durable
  `sandbox.status` envelopes, which the API applies with the same
  `record_transition` logic, so the `environment.*` events look the
  same to clients; the periodic seen update travels as an idempotent
  `sandbox.seen` summary that the API validates against the
  connection's leases. The wipe after `session.stop` and the idle
  workspace wipe travel as `session.stopped` and `workspace.reaped`
  receipts; the worker wipes only its local directory, and only
  `session.stopped` deletes the session blobs on ingest
  (`workspace.reaped` is a receipt only). The reaper uses the command
  context TTL, and sessions it does not know wait for the inventory
  reply, which carries the effective idle TTL per session, instead
  of reading the database. Lifecycle export moved to the API: the
  worker reports live start and stop as durable `lifecycle.start`
  and `lifecycle.stop` envelopes (buffered across disconnects and
  replayed exactly once), the periodic `inventory` live set is what
  the API derives heartbeats from, and retry and backoff stay on
  the API. `apipi worker` ignores `APIPI_LIFECYCLE_*` settings with
  a startup warning. On hello and about every 60s the worker sends
  `inventory{sessions}`; the API fails leases it no longer reports
  (`agent.session.error` with code `worker_orphaned`, then clears
  the lease) and revokes sessions the worker reports without a
  lease. A reconnect to any replica renews the reattached leases,
  so running turns stay alive and the lease moves to the new
  replica. Commands are idempotent by `command_id`: a retransmitted
  `turn.start` is acked but never dispatched twice. See
  `docs/workers.md` (inventory, lease takeover), `docs/usage.md`
  (lifecycle export), and `docs/config.md` (lifecycle settings are
  API-only). No new migration.
- Review round 2 on top: an unleased on-disk workspace whose
  session row is still alive gets a TTL answer (a released lease
  only means the Pi stopped; the workspace stays until its idle
  TTL) and is revoked only once the row is gone. The TTL answer
  also carries the idle baseline (`idle_since_epoch`, the row's
  last touch) so re-answering it every inventory does not restart
  the reaper clock. Only `session.stopped` deletes the session
  blobs; `workspace.reaped` is a receipt only, so a session leased
  again before its receipt lands keeps its artifacts.
- Review round 1 on top: command dedupe is worker-lifetime (a replay
  after reconnect is still recognised; ids are forgotten on
  teardown, stop, or revoke). Lease takeover renews exactly the
  reported leases with a conditional `UPDATE` matching `worker_id`
  and `lease_id` (register already points
  `workers.api_instance_id` at the new replica); heartbeats still
  extend all of the worker's leases. The orphan rule spares leases
  with a command still unacked (marked before the grant commits, so
  a grant in flight cannot orphan). Lifecycle identity always comes
  from the session row (the worker only adds sandbox, run mode, and
  timing fields, which also keeps the strict payload validation
  happy), and exports are deferred past the ingest commit so a
  replayed batch cannot export twice. Sandbox phase updates live in
  one shared function used by the local transition path and the
  ingest. The inventory also reports on-disk workspaces the worker
  holds no lease for: rows that are still alive get a TTL answer so
  the reaper wipes them when the TTL runs out, and only a missing
  row gets a revoke that wipes the directory at once.
- Workers run without database or object-store credentials, with TLS on the worker socket (#450). `run_worker` no longer creates a `Store`: the turn runtime resolves every write through the result sink, so a worker (outbox sink, turn context in every command) never touches the database, and the worker event bus is always in memory. A command without a turn context fails fast on a database-less worker instead of reading the store. `apipi worker` refuses to start when `DATABASE_URL` is set in the environment or `database_url` is set in the config file, and `prepare_worker` no longer warns about the vault master key, since workers never decrypt vaults. TLS is required for non-loopback API URLs (`https://` or `wss://`; loopback `http://` stays allowed for local development), and mutual TLS is optional via `APIPI_WORKER_CLIENT_CERT` / `APIPI_WORKER_CLIENT_KEY` with an optional `APIPI_WORKER_SERVER_CA` bundle for the API server certificate (terminate TLS and verify the client certificate on the proxy). A restarted worker keeps adopting the API persisted cursor from `hello.reply` (`outbox.set_base`, #446); the split-mode end-to-end suite now covers that restart path, so replayed sequence numbers are never mistaken for duplicates. The split-mode end-to-end suite runs full turns with tools and MCP, artifacts, cold restore, and streaming with the worker constructors blocked. This resolves #441 item 3. See `docs/workers.md` (What runs where, Transport security), `docs/scale.md`, `docs/worker-concepts.md`, `docs/production.md` (token rotation, transport security), and `docs/config.md`.

### Changed

- Pi upgraded from 0.99.1 to 1.0.0. All guest images (default, browser, work) are rebuilt as a new store version; mirror or pull them before upgrading workers.
- MCP: model-facing Pi tool names now use `_` instead of `-` from the server label (`mcp__my_server__tool`). `server_label` in responses is unchanged.
- MCP tools whose `server_label`s differ only in `-` vs `_` are rejected with `400` and code `mcp_label_collision`, on agent create, agent update, and inline session agents.
- Codemode: `models.generateImages()` (new in Pi 1.0.0) is unsupported and untested, like `models.classify()`.

### Fixed

- A follow-up turn no longer hangs until the turn timeout when the wall clock steps back between two turns (#506). The newest turn is found by `created_at`, and a clock step (WSL2 time sync, NTP) gave the follow-up turn an earlier stamp than the turn before it, so every envelope of the follow-up was rejected as `turn_mismatch`. A new turn now always gets a stamp after the previous turn of its session, which also keeps the turn list in creation order.
- Postgres event bus: with a store passed in (the API always does), the bus read the database URL with the password masked and could not log in. Cross-replica wake-ups and command forwarding notifications now work on a Postgres with a password.
- A turn whose lease is orphaned (the worker came back without it) is failed at once instead of staying `in_progress` until the turn timeout. A restarted worker now sends its first inventory as soon as its outbox is acked (2 to 30 seconds after connecting) instead of after 60 seconds.
- Worker protocol cleanups for the spec (#489). The error object the API sends before it closes a socket is now `{"type": "error", "ok": false, "error": <text>, "code": <close reason>}`, and a first message that does not arrive within 15 seconds is answered with it (`register_timeout`) instead of a bare close. The worker decides on `code` and still understands an older API. The legacy top-level `event` message (`WorkerEventMessage`) is removed: it had no sender, and a worker reports events as `event` envelopes. `artifact.presign.reply` writes `expires_at` as RFC 3339 UTC with `Z` and milliseconds, as the other times on the wire do.
- Worker protocol delivery guarantees and forward compatibility (#488). Ingest separates temporary from permanent failures: a deadlock, a lock or statement timeout, a connection reset, or a temporary object-store error (also on `artifact.completed`) no longer becomes `ingest_error` and no longer moves the ack past the envelope. The API tries the batch again, closes the socket as `ingest_failed` if it keeps failing, and the worker replays. Permanent rejects are still acked past. Receivers ignore unknown fields in envelope payloads, control messages, command payloads, and the command context, and count them (`apipi_worker_protocol_total{event="unknown_field"}`); unknown message types and command ops are logged and counted (`unknown_type`, `unknown_op`) and are never acked as done. `register` and `hello` carry `features` (`search`, `presign`, `lease_cursor`, `session_stopped`); a peer without `features` is a baseline peer. A replayed `artifact.presign` gets the same reply again (migration `0029` stores the `request_id` with the upload slot), and the reply is sent before the ack. Commands are kept per lease in order, are sent again on every reconnect and every 5 seconds until acked, and fail after the lease TTL with `worker_command_timeout`; a lease whose granting command never reached the worker is no longer kept alive forever. `session.stop` is acked on receipt (with the `session_stopped` feature) and completes with the durable `session.stopped` envelope, which the API waits for before it drops the lease. A restarted worker that claims no sessions keeps its leases until its first inventory, so its spooled results are applied. Size limits are exact bytes of the UTF-8 JSON frame: `MAX_MESSAGE_BYTES` is 1,048,576 and `MAX_COMMAND_BYTES` is 262,144 (they were 1,000,000 and 256,000). New failure codes `worker_command_timeout`, `worker_outbox_full`, and `worker_message_too_large` in the failure table, and new log events. See `docs/workers.md` (Delivery guarantees), `docs/observability.md`, and ADR 0015.
- Worker protocol, worker side (#487). The receive loop no longer waits: commands, `session.stop`, revokes, and the teardown and artifact harvest behind them run as tasks, so presign replies, acks, and pongs keep flowing. The outbox pump sends each envelope once per connection and resends the unacked backlog only after a reconnect (`apipi_worker_replayed_total` counts real resends); the disk spool is append-only, fsynced once a second, and compacted in a thread. A drain waits until the API acked the outbox (bounded by `--drain-timeout`, also while disconnected), a killed session is harvested only while the socket is open, a presign waiter fails when the socket closes after the API acked its request, and a cancelled turn is no longer turned into an upload error. The worker pings every 5 seconds (10 second timeout), waits 15 seconds for `hello.reply`, reconnects with jittered backoff (cap 10 seconds) and a fresh TLS context, and reconnects on a first frame that is not `hello` unless the API rejected it. Malformed frames are skipped, a failing command is answered with an `error` and retried on retransmit, a lease is tracked only for an accepted command, dedupe entries and empty outbox buffers are removed when a session ends, one session may use half of the outbox bounds, and an envelope over 1,000,000 bytes fails the turn with `worker_message_too_large`. New reconnect reasons `ping_timeout` and `hello_timeout`, delta drop reason `oversize`, and log events `worker.drain.waiting`, `worker.harvest.skipped`, `worker.outbox.oversize`, `worker.outbox.spool_error`, and `worker.message.failed`. See `docs/workers.md`.
- Worker protocol (#483): three bugs that could lose a running turn or its results without an error. A busy worker keeps its leases: heartbeats and the inventory run on their own timers instead of waiting for a quiet socket, and `hello.reply` now carries `lease_ttl_seconds` and `heartbeat_seconds` from the API (a worker-side `APIPI_WORKER_LEASE_TTL` is ignored with a warning, and a `hello` without these fields stops the worker, so upgrade the API before the workers). The API also renews leases on command acks and committed ingest batches. A turn on a new lease continues the session sequence: `turn.start`, `turn.continue`, and `sandbox.boot` carry `payload.last_seq`, so a second worker or a restarted worker no longer has its first envelopes dropped as duplicates until the turn times out. `lease.release` and the `session.stop` ack now wait until the API acked the envelopes buffered for the session, so `lifecycle.stop`, the last `sandbox.status`, harvested artifacts, and `session.stopped` are no longer rejected as `not_leased`; `session.stop` runs as its own task, so artifact presign replies during a stop are no longer blocked. A rejected envelope from a worker that lost the lease no longer moves the session cursor, and `workspace.reaped` is acked without a lease. New series `apipi_worker_heartbeat_gap_seconds`, `apipi_worker_lease_events_total`, `apipi_worker_ingest_total`, and `apipi_worker_ingest_rejected_total`, and the log events `worker.heartbeat.late`, `worker.ingest.duplicate`, `worker.release.unflushed`, `worker.command.cursor_missing`, and `worker.lease_ttl.ignored`. `worker.lease.expired` now carries the age of the last renewal. See `docs/workers.md` and `docs/observability.md`.
- Codemode scripts can call MCP tools and `web_search` again (#481). ApiPi enabled codemode with a `--tools read,bash,edit,write,codemode` allowlist, which also hid every extension tool, so `tools.mcp__<server>__<tool>` failed with "does not exist" under `apipi.codemode=on` and `only`. ApiPi now writes `defaultTools: ["+codemode"]` into the Pi `settings.json` instead and passes no `--tools`.
- Artifacts published by a worker keep their guessed content type (`text/plain`, `text/html`, and so on) again. The worker upload sent no content type, so every artifact came back as `application/octet-stream`.
- The early `400` for `packages.system` on hosted microVM sessions now fires on the API. `openai_hosted` is always placed on `microvm`, so the API rejects `system` packages whenever `type=openai_hosted`, whatever run mode the API runs with. Before, an API without a local run mode accepted the request and the turn failed later on the worker.
- Split mode: user cancel was silently dropped. `turn.cancel` was sent without `tenant_id`, which the worker keys on, so cancelling a running turn never aborted it. A follow-up message on a session whose worker no longer runs the turn (worker restarted, stale `in_progress` row) now releases the lease and fails the stale turn within a short bounded wait (`CANCEL_GRACE`, 5 s) instead of blocking until `turn_timeout`; a live lease whose worker socket is on another API replica is kept and answered with the usual 429. `RemoteExecution._wait` now returns after `agent.session.idle` for completed, cancelled and failed turns, so the POST no longer reports `in_progress` while the status envelope is still in flight.
- Worker protocol, API side (#486): a worker socket stays up and keeps its leases under load and during short database or network problems. Each socket now runs a control lane (heartbeat, `lease.ack`, `lease.release`, inventory, `sandbox.seen`), an ingest lane, and a delta lane, so a large artifact, a slow object store, or a burst of deltas no longer delays heartbeats. All sends go through one writer per connection with a bounded queue and a 10 second write timeout (disconnect reason `write_timeout`). An error on one message is logged and counted and no longer closes the socket, and binary or invalid JSON frames are skipped. A connection becomes pickable only after `hello.reply` is queued. Delta "turn done" checks use in-memory state fed by ingest instead of one query per delta. Presign and `artifact.completed` read and hash objects in chunks, in a thread, before the session row lock is taken, and the filesystem store reads in a thread. The inventory TTL lookup is a constant number of queries. `pg_notify` publishes are serialized, and any publish error is caught, logged, and counted. The lease reaper sends revokes after the commit, with a timeout, through the writer. A heartbeat with an invalid optional field still extends the leases. Detach clears `api_instance_id` only for the current connection, and a heartbeat from a superseded `generation` renews nothing. uvicorn runs with `ws_max_size` 4 MiB. New `apipi_worker_protocol_total` events and log events are in `docs/observability.md` and `docs/usage.md`, the disconnect reasons `ping_timeout`, `write_timeout`, and `ingest_failed` are now used, and `docs/production.md` has a sizing rule for the database pool. See `docs/workers.md` and ADR 0015.

### Breaking

- Combined `apipi serve` (API plus in-process Pi) is removed. `apipi serve` is always the API: it runs no Pi, no sandbox, no run-mode probe, and no run-mode check. `apipi worker` runs Pi, and `apipi dev` runs both locally. `--api-only`, `APIPI_API_ONLY`, and the `api_only` TOML key are removed, and `apipi check --role` is required and accepts only `api` and `worker`, so a bare `apipi check` now fails. `apipi serve` no longer logs the `none` run-mode warning (the worker still does). See the upgrade notes.
- The `/v1/apipi/chat` facade is removed (all 11 routes return `404`). Text-only sessions are Agents API sessions with `environment.type=none`, which allows function tools and HTTP MCP with `server_url` only (anything else is `400`). The `chat` run mode, `src/apipi/worker/pi/isolation/chat.py`, the `apipi.session_kind` metadata key, and `APIPI_ENV_NONE_PLACEMENT` / `[placement].env_none` are removed. Saved agents that still carry `apipi.session_kind=chat` are ignored for placement; bundle import drops the key with a warning and exports no longer carry it. Workers now advertise an accepts set with `APIPI_WORKER_ACCEPTS` / `[worker].accepts` (comma list from `none`, `microvm`; default `none,microvm` on a microVM backend, else `none`). If `microvm` is listed but the microVM backend cannot run, the worker fails fast before it registers. Pi for `type=none` always runs directly on the worker host. `placement_for` returns `none` for `type=none` and `microvm` otherwise; `pick` uses set membership with least-loaded logic and no fallback (`429` `capacity`). Migrate:

  | Old | New |
  | --- | --- |
  | `POST /v1/apipi/chat/sessions` | `POST /v1/agents/sessions` with `"environment": {"type": "none"}` |
  | `GET /v1/apipi/chat/sessions` | `GET /v1/agents/sessions` |
  | `GET` / `POST` / `DELETE /v1/apipi/chat/sessions/{id}` | the same verbs on `/v1/agents/sessions/{id}` |
  | `POST` / `GET /v1/apipi/chat/sessions/{id}/events` | `/v1/agents/sessions/{id}/events` |
  | `GET /v1/apipi/chat/sessions/{id}/turns[/{turn_id}]` | `/v1/agents/sessions/{id}/turns[/{turn_id}]` |
  | `GET /v1/apipi/chat/sessions/{id}/items` | `/v1/agents/sessions/{id}/items` |
  | `GET /v1/apipi/chat/sessions/{id}/export` | `GET /v1/apipi/sessions/{id}/export` |
  | `APIPI_RUN_MODE=chat` workers | workers with `APIPI_WORKER_ACCEPTS=none` |
  | `APIPI_ENV_NONE_PLACEMENT` | removed; `type=none` goes to any worker whose accepts set contains `none` |
  | `apipi.session_kind=chat` | `environment.type=none` |
- `environment.type=self_hosted` is not supported for now. Session create, agent `session_defaults`, and template import with that type return `400` with type `not_implemented` and the message `environment type self_hosted is not supported`. The runner WebSocket (`/v1/environments/{environment_id}`), `EnvironmentHub`, runner client (`examples/self_hosted_runner.py`), `APIPI_SANDBOX_TTL_SELF_HOSTED` / `[sandbox.ttl].self_hosted`, and the `self_hosted` prompt fragments are removed. `GET /v1/agents/environments/{id}` stays for hosted computers. The type may come back later on worker protocol v2. Use `openai_hosted` as the workaround.
- Old route aliases outside `/v1/apipi` are removed and return `404`. Routers now serve directly under `/v1/apipi/...`. Migrate:
  - `GET /v1/usage` -> `GET /v1/apipi/usage`
  - `POST /v1/templates`, `POST /v1/templates/import`, `GET /v1/templates`, `GET /v1/templates/{id}`, `GET /v1/templates/{id}/download`, `DELETE /v1/templates/{id}`, `POST /v1/templates/{id}/agents` -> the same paths under `/v1/apipi/templates/...`
  - `POST /v1/uploads`, `POST /v1/uploads/{id}/complete` -> `POST /v1/apipi/uploads`, `POST /v1/apipi/uploads/{id}/complete`
  - `/v1/chat/sessions...` -> `/v1/apipi/chat/sessions...` (the `/v1/apipi/chat` routes themselves are unchanged)
  - `GET /v1/agents/{id}/export` -> `GET /v1/apipi/agents/{id}/export`
  - `GET /v1/agents/sessions/{id}/export` -> `GET /v1/apipi/sessions/{id}/export`
  - `POST /v1/agents/sessions/{id}/artifacts/{artifact_id}/download` -> `POST /v1/apipi/sessions/{id}/artifacts/{artifact_id}/download`
  - `POST /v1/files/{id}/download` -> `POST /v1/apipi/files/{id}/download`
  - `POST /v1/skills/{id}/download` -> `POST /v1/apipi/skills/{id}/download`
- Agent versions and snapshots are removed. The versions routes
  (`/v1/apipi/agents/{id}/versions`, including create, list, get,
  restore, and delete) are gone and return `404`, and
  `APIPI_AGENT_VERSIONS_KEEP` is removed (still setting it logs a
  warning and is ignored). Migration `0022` drops the
  `agent_versions` table, so existing snapshots are deleted. Export
  any you need before upgrading. To keep snapshots, use the agent
  bundle export (`GET /v1/apipi/agents/{id}/export`) and store the
  zip yourself.
- Agent `revision` is removed from the agent object, and the
  `x-apipi-agent-revision` model-host header is no longer sent.
  `x-apipi-session-id`, `x-apipi-turn-id`, and `x-apipi-agent-id`
  are unchanged.
- Agent export no longer takes `?version=`, and new bundles have no
  `source_version` (import still accepts old bundles and ignores
  it).
- Pi's built-in MCP client is the only client. The gateway no longer
  probes MCP servers at session create: the shape, headers, vault,
  and SSRF guard are still checked there, but a dead server or one
  that rejects unauthenticated calls no longer fails session create.
  Server failures surface at turn time as `pi.extension_error` and do
  not fail the turn. The per-turn `mcp_list_tools` items are gone.
  Agent sessions rebuild the server list from the live agent at every
  turn, so agent edits apply to the next turn without recreating the
  session.
- `APIPI_ERROR_CODES` now defaults to `specific`. `agent.session.error` and the non-stream `502` body carry the specific failure code in `code` (copied in `detail_code`), with `legacy_code` still `model_host_error` on upstream failures. Set `APIPI_ERROR_CODES=legacy` to keep `model_host_error` in `code` for one release. Clients that match `model_host_error` on `agent.session.error` or the `502` body should match the specific code (or `detail_code`) instead.
- The legacy guest-image cache (`~/.cache/apipi/microvm/{vmlinux,rootfs.ext4,rootfs-browser.ext4}`) is removed. The image store is the only source: run `apipi images pull <id>`. `APIPI_MICROVM_IMAGE` / `[sandbox].image` and `APIPI_MICROVM_ROOTFS_BROWSER` / `[sandbox].rootfs_browser` are removed; `apipi microvm shell` and `apipi install` take the image from `[sandbox].default_image` or `--image`. `APIPI_MICROVM_KERNEL` / `[sandbox].kernel` and `APIPI_MICROVM_ROOTFS` / `[sandbox].rootfs` stay as dev-only overrides. `apipi install --microvm --build` is removed; use `apipi images build`, `apipi images push --store-version <v>`, and `apipi images pull`. `apipi images publish` is removed (use `push --store-version`). Push without `--store-version` now fails instead of writing a deprecated schema-1 store. Migration: `apipi images pull <id>`.
- `metadata["apipi.sandbox_size"]` is removed: session create/update, agent create/update, and template/bundle import with that key return `400` (set `environment.container_size` (`small` / `medium` / `large`) or `environment.sandbox_size` (`S` / `M` / `L`)). `container_size` is the input and is stored as `sandbox_size`. Bundle import migrates the key to `session_defaults.environment.sandbox_size` with a warning when the environment has no size. `metadata["apipi.sandbox_image"]` stays as the stock-SDK path for `environment.sandbox_image`. `metadata["apipi.thinking"]` is removed as client input (`400`, set `reasoning.effort` with `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`); the gateway still stores the resolved level under that key but strips it from public session/agent `metadata` (the `reasoning` field shows the level, so echoing returned metadata back is safe). Bundle import migrates the key to `reasoning.effort` with a warning, and bundles carry the level in `reasoning`. The `idle_ttl` field wins over `metadata["apipi.idle_ttl"]`, which stays for stock SDKs. Eager boot is session metadata, then agent metadata, then `APIPI_SANDBOX_EAGER_BOOT` (the `session_defaults` lookups are removed). `hosted` is normalised to `openai_hosted` on input; downstream checks only accept `openai_hosted`. The internal setting `workspace_ttl` is renamed to `sandbox_ttl_openai_hosted` (env `APIPI_SANDBOX_TTL_OPENAI_HOSTED` and `[sandbox.ttl].openai_hosted` are unchanged).
- Prompt fragments move into `[pi.prompts]`. The `APIPI_PLATFORM_*` fragment env vars (including `*_FILE`) are removed; set `[pi.prompts]` keys (`identity.none`, `identity.computer`, `main.none`, `main.hosted`, `additional.none`, `additional.hosted`, `capability`, `browser`, `mcp_tool`) instead. The `size`, `network`, `network.enabled`, and `network.restricted` fragments and their prompt files are removed, with `network_hint`, `sandbox_size_hint`, and the `settings is None` prompt branches. `APIPI_PLATFORM_PROMPT`, `APIPI_PLATFORM_PROMPT_ADDITIONAL`, `APIPI_PI_SYSTEM_PROMPT`, and `metadata["apipi.system_prompt"]` are unchanged.
- Small cleanups: Playwright MCP checks are removed (`mcp_playwright_*`, `/opt/apipi/playwright-mcp`, `APIPI_SANDBOX_AUTO_PLAYWRIGHT`, `[sandbox.browser]` keys). Stdio MCP helpers (`tests/support/mcp_stdio*.py`) are removed; MCP is HTTP only. `APIPI_WORKSPACE_TTL` / `workspace_ttl` is removed (use `APIPI_SANDBOX_TTL_OPENAI_HOSTED`). Flat TOML keys no longer warn (use `[pi]`, `[sandbox]`, `[mcp]` tables). `host` / `jail` run modes fail with the generic run-mode message. `worker_memory_mb` defaults in the validator; `node_memory_mb()` assumes it is set. The Pi session cache keeps only `pi_session_id` (migration `0024` drops `sessions.pi_session_uri`). Dead code is removed (`identity_from_result`, `get_db`, `env_spec_or_none`, `StaticBearerAuth`, `ImageStore` base, `part_name`, `allowed_egress_ips`, `pi_binary`, `require_size_rootfs`, the `Execution` protocol, and package re-exports). Test-only helpers (`sweep_host_orphans`, `get_item`, `artifact_blob_path`, `split_bytes`, `verify_checksums`) are removed with their tests. `artifact_blob_uri` / `read_blob_uri` are removed now that the session cache keeps only `pi_session_id`.
- Worker protocol v2 is a hard cut. The first worker message must be `register` with `protocol: 2`; anything else closes the socket with code `1008` and reason `unsupported_protocol`. The API answers with `hello.reply` (`protocol`, `worker_id`, `generation`, `sessions: {id: last_seq}`). Envelope and message schemas live in `src/apipi/worker/protocol.py`, shared by the API and the worker. Upgrade the API and all workers together. Rejections are logged and counted in `apipi_worker_protocol_total`. See [ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md) and [workers](docs/workers.md).
- The shared `APIPI_WORKER_TOKEN` is removed with no fallback. Setting it fails API and worker startup with a message that points at `apipi workers token create`. Migration: run `apipi migrate` (migration `0025` adds the `worker_tokens` table), create one token per worker with `apipi workers token create --name ...` (the secret prints once, starts with `apipi_wk_`, and only its SHA-256 hash is stored), write each secret to a file on its worker host, and set `APIPI_WORKER_TOKEN_FILE` to that path. A token is bound to one `worker_id` on first register (or with `--worker-id` at creation); a register without `id` is assigned the bound id, and a different explicit `id` is rejected with `token_bound`. A token is valid only on `/internal/worker` (`401` everywhere else, recognised by prefix without a database lookup), and several active tokens per worker allow rotation without downtime (`create`, roll out, `revoke`; revocation closes live sockets). See [workers](docs/workers.md) and [production](docs/production.md#worker-token-rotation).
- Workers no longer take `DATABASE_URL`. `apipi worker` refuses to start when `DATABASE_URL` is set in its environment or `database_url` is set in the worker config file: unset it on worker hosts, since only the API connects to Postgres. The worker also needs no object-store credentials (artifacts move through API-issued presigned URLs or the shared store root). `prepare_worker` no longer warns about an unset vault master key. Migration: remove `DATABASE_URL` from the worker environment (systemd unit, Compose service, or shell profile) and from the worker config file, and point the worker at the API with `APIPI_API_URL` plus its token file.
- Worker sockets require TLS for non-loopback API URLs. `apipi worker` fails at startup when `APIPI_API_URL` is a non-loopback plain `http://` or `ws://` address: serve the API behind `https://` (or `wss://`) with a trusted certificate. Loopback `http://` URLs stay allowed for local development. Migration: change the worker `APIPI_API_URL` to the `https://` API endpoint before upgrading. Optional mutual TLS (`APIPI_WORKER_CLIENT_CERT` / `APIPI_WORKER_CLIENT_KEY`, plus `APIPI_WORKER_SERVER_CA` for a private API CA) is new and off by default. See [workers](docs/workers.md#transport-security) and [production](docs/production.md#worker-transport-security).
- `environment.type=none` tool and metadata checks now apply to every `type=none` session, not only chat sessions. Built-in tools are always off (`apipi.builtin_tools=on` is `400` with code `builtin_tools`), `apipi.codemode` `on` or `only` is `400` with code `builtin_tools`, and only function tools and HTTP MCP with `server_url` are allowed (anything else is `400` with code `tool_not_allowed`, renamed from `chat_tool`; update clients that match `chat_tool`). The checks run on agent create and update (when the agent's `session_defaults.environment.type` is `none`), on session create (using the effective environment, tools, and metadata, so a hosted-saved agent that conflicts with `type=none` cannot be used for one), on session update (whenever metadata changes on a `type=none` session), and on template import. `apipi.codemode` `on` or `only` together with an effective `apipi.builtin_tools=off` is `400` with code `builtin_tools` everywhere. Template import also stops injecting empty `skills`/`files` lists into `session_defaults.environment`, so `type=none` defaults validate instead of failing on hosted-only fields.

### Security

- MCP headers no longer expand `${ENV}` from the gateway process.
  Values that contain `${...}` are rejected at session create, so a
  tenant can no longer have gateway environment values (for example
  `APIPI_VAULT_MASTER_KEY` or `DATABASE_URL`) sent to a server of
  their choice. Store MCP secrets in a vault credential
  (`static_bearer` bound to `mcp_server_url`) and attach `vault_ids`
  on the session. `examples/tavily.yaml` now shows that shape.
- MCP `server_url` targets pass an SSRF guard on the gateway connect
  and on every host broker call. Loopback, RFC 1918, link-local
  (including cloud metadata), CGNAT, ULA, multicast, reserved, and
  other special-use addresses are rejected, including hostnames that
  resolve to them. Operators with an MCP server on a private address
  (including local development on `127.0.0.1`) list it in the new
  `APIPI_MCP_ALLOW_HOSTS` / `[mcp].allow_hosts` setting (hostnames or
  CIDRs, empty by default).

### Upgrade notes

- Combined `apipi serve` (API plus in-process Pi) was removed. `apipi serve` is always the API; run `apipi worker` next to it, or `apipi dev` locally. `--api-only` and `APIPI_API_ONLY` were removed. A leftover `APIPI_API_ONLY` in the environment, or `api_only` in the TOML file, makes `apipi serve` and `apipi worker` log `config.api_only_removed` and continue. Drop `--api-only` from unit files and container commands, and set `APIPI_RUN_MODE` on the worker instead of on the API.
- Replace `${ENV}` in MCP tool headers with a vault credential and
  `vault_ids` on the session. Sessions that still send `${...}`
  headers fail with a message that names the vault path.
- MCP servers on private addresses need `APIPI_MCP_ALLOW_HOSTS` /
  `[mcp].allow_hosts`, or session create fails with a blocked-host
  message.

## [0.13.0] - 2026-09-30

### Added

- Gateway auth is bounded and revocable. `APIPI_AUTH_CACHE_MAX`
  (default `10000`, `0` disables caching) adds LRU eviction to the
  auth cache. `Gateway.invalidate_auth`,
  `Gateway.invalidate_auth_where`, and `Gateway.clear_auth_cache`
  drop entries in process, and
  `POST /v1/apipi/auth/invalidate` drops the caller's tenant entries
  over HTTP. The auth plugin may be `async def` or sync (sync runs in
  a worker thread), concurrent misses for one key call the plugin
  once, and the tenant lookup is memoized.
- Optional `APIPI_AUTHORIZE` hook (`authorize=` on `Gateway.create`)
  enforces resource-level authorization with stable action names
  (`agent.read`, `agent.write`, `agent.list`, `agent.run`,
  `session.read`, `session.list`, `vault.*`, `file.*`, `skill.*`,
  `template.*`, `usage.read`, `auth.invalidate`). List actions accept
  `AuthFilter(ids)`. Missing resources stay `404`; denied resources
  are `403`. Existing sync `authenticate` plugins now run in a
  thread; plugins that relied on running on the event loop thread
  should become `async def`.
- Model-host attribution. The per-session broker stamps
  `x-apipi-session-id`, `x-apipi-turn-id`, `x-apipi-agent-id`, and
  `x-apipi-agent-revision` on every model request and strips
  guest-set `x-apipi-*` headers. Agents gain a read-only `revision`
  (`1` on create, plus one per update and restore). New
  `APIPI_MODEL_ATTRIBUTION_HEADERS` (default `true`) controls
  stamping.

### Breaking

- Pi 0.99.1 (was 0.85.1). All guest images are rebuilt with a new
  store version; operators must mirror and pull again.
- MCP runs on Pi's built-in MCP client over streamable HTTP.
  Model-facing tool names change from `mcp_<server_label>_<tool>` to
  `mcp__<server_label>__<tool>`, sanitised and hashed when long. API
  output items still carry `server_label` and the original tool name.
  `server_label` must match `[A-Za-z0-9_-]`. New `mcp_list_tools`
  output items list each server's tools at turn start. New
  `allowed_tools` support restricts the tools. Other
  `require_approval` values, `connector_id`, and `authorization` are
  `not_implemented`.
- MCP tool input is the flat OpenAI format only: `server_url` at top
  level, plus `allowed_tools`, `require_approval` (only `never`), and
  `server_description`. The nested `transport: {...}` shape is removed
  and returns `unknown_field`. Stored agents and sessions with the
  nested shape must be recreated; using them fails with a clear error.
- stdio MCP is removed. `transport: {type: "stdio"}` tools are no
  longer accepted, and `stdio_on_host` is gone from the isolation
  plugin interface.
- A slash-command input (for example `/mcp`) that Pi handles as a
  command ends the turn with an error instead of hanging.
- New opt-in `apipi.codemode` (`off` | `on` | `only`, default `off`).
- Pi starts with an explicit extension list and `PI_OFFLINE=1`.
  Workspace `.pi/` project resources are never loaded.

## [0.12.1] - 2026-09-30

- The official image publish is x86_64 `default` and `browser` only.
  It does not build `work` or aarch64. Build those locally if you need
  them.

### Fixed

- The aarch64 Node tarball pin matches nodejs.org. A local aarch64
  image build no longer fails the checksum.
- The Images workflow runs only on a release tag. Dispatch it with
  `--ref vX.Y.Z`. A branch dispatch fails before it builds or signs.
- `apipi images verify` and `apipi images mirror` accept
  `--signer-identity` and `--signer-issuer` for a store that was not
  signed by the tag. The 0.12.0 store was signed from `main`.
- A release that does not change image inputs reuses the previous
  store and signs it for the new tag. It does not rebuild the guest.
- `APIPI_SANDBOX_IMAGE_MIN_VCPUS` overrides the per-image vCPU floor.
  Unset still uses the recipe or manifest value. A value below the
  recommended floor logs a warning.
- A log flush failure (for example a closed test capture stream) no
  longer propagates out of the logging handler, so it cannot kill the
  request or socket handler that logged.

## [0.12.0] - 2026-09-30

The 0.12.0 image store was not signed by the release tag. Use 0.12.1
for official images.

### Breaking

- The image store is versioned per release. `image_source` is the base
  above `v<version>/` prefixes. Workers pull that store version, which
  defaults to the running ApiPi version, instead of `latest` per image.
  Set `APIPI_IMAGE_STORE_VERSION` to pin or roll back. The official
  store is the GitHub release at
  `https://github.com/GEKI-AI/apipi/releases/download/v<version>/`.
- Operators mirror and verify that store, then pull. `apipi images
  push` without `--store-version` writes a deprecated schema 1 store
  and warns. `--force` is refused for a complete versioned prefix.
- The local kernel layout gains `kernels/<arch>/<kernel_version>/vmlinux`.
  `kernels/<arch>/vmlinux` remains a compatibility copy.
- All guest images (`default`, `work`, `browser`) now use Debian
  trixie slim instead of Alpine. Operators must re-pull or rebuild
  every image. 0.11.x images and manifests are not compatible.
- Alpine support is removed from the image build. There is no
  `ALPINE_VER`, minirootfs, or `apk`. Custom recipes must use Debian
  package names in `PACKAGES`.
- Environment `packages.system` now means Debian/apt package names.
  Alpine names such as `py3-*` and `font-*` no longer work. The
  restricted-network egress allowlist for system packages uses the
  Debian mirrors.
- Guest Python is 3.13. Node comes from a pinned nodejs.org tarball.
  The guest uses glibc. `ripgrep` (`rg`) is in every image.
- Images are larger. Check rootfs disk space on workers.
- The `browser` image is rebuilt on agent-browser and
  chrome-headless-shell, and is x86_64 only. An aarch64 worker does
  not offer it. Size `L` or `image: browser` on aarch64 is
  `image_unavailable`. `default` and `work` stay multi-arch.
- Playwright MCP is removed. `mcp_playwright_*` tools no longer exist.
  Use bash and `agent-browser` through the built-in `browser` skill.
- `APIPI_SANDBOX_AUTO_PLAYWRIGHT` and `[sandbox.browser]` are warned
  about and ignored.
- Prompt overrides `APIPI_PLATFORM_PLAYWRIGHT`,
  `APIPI_PLATFORM_PLAYWRIGHT_FILE`, `APIPI_PLATFORM_CHROMIUM`, and
  `APIPI_PLATFORM_CHROMIUM_FILE` are removed.
- Browser VMs get at least 2 vCPUs, including size `M`.
- The browser no longer writes into `/workspace/outputs` by default.
  Only artefacts the user explicitly asked for go there.
- `examples/playwright.yaml` is removed. Browser examples use
  agent-browser.

## [0.11.0] - 2026-09-29

### Breaking

- Agent versions are explicit snapshots. Create and update no longer
  write a version. `active_version` is gone from agent responses.
  `agent_version` is gone from session, turn, usage, and lifecycle
  bodies. `metadata["apipi.agent_version"]` is ordinary metadata.
  `POST /v1/apipi/agents/{id}/versions/{version}/activate` is removed.
  Version create no longer accepts `definition` or `activate`, and
  versions have no `status`. `note` is replaced by `name` and `comment`.
  Sessions follow the live agent again, so an edit changes the next turn
  of a conversation that already exists. See
  [agent versions](docs/agent-versions.md).
- A database that already applied the 0.10.x `0019_agent_versions`
  migration must be recreated. That revision was rewritten in place.

### Added

- `POST /v1/apipi/agents/{id}/versions/{version}/restore` copies a
  snapshot back onto the agent and first saves the live definition as a
  `pre_restore` snapshot.

### Changed

- `APIPI_AGENT_VERSIONS_KEEP` defaults to 10.

### Fixed

- Registry `thinking_levels` follows Pi's map: a missing standard
  level is allowed, `null` is unsupported, and `xhigh` or `max` need
  an explicit string. A missing `models.json` still includes the
  session model and registry capabilities. `service_tier` of `null` or
  `auto` is ignored.
- An agent update that sends only `reasoning` keeps the other metadata
  keys. A session update can change `reasoning.effort`, including back
  to the agent or model default. A 400 for a disagreeing level happens
  only when the same request sets both `reasoning.effort` and
  `metadata["apipi.thinking"]`.

## [0.10.1] - 2026-09-29

### Fixed

- The wheel build no longer lists `src/apipi/worker/pi/prompts` in
  `force-include`. Those files are already in the package, and the
  duplicate stopped the 0.10.0 publish.

## [0.10.0] - 2026-09-29

### Added

- Agent versions. A session keeps the version it was created with.
  Move a session by setting `metadata["apipi.agent_version"]` while it
  is idle. See [agent versions](docs/agent-versions.md).

### Changed

- An agent edit no longer changes sessions that already exist.
- Guest kernel is Linux 6.1.186 from the Firecracker 1.17 CI set,
  with virtio-rng. The old quickstart 4.14 kernel is no longer
  downloaded.
- Browser sessions pass Chromium launch flags that skip first-run
  network, and a 30 second navigation timeout. Guest `mcp:` lines are
  logged at info, including a timed-out tool name and duration.
- Pi prompt text ships as files under `src/apipi/worker/pi/prompts/`
  and is read at startup. The identity line names Pi as the harness.
  `APIPI_PLATFORM_NAME` is that name. The hosted prompt no longer states
  a sandbox timeout. User-provided files under `inputs/` are restored.
  Other workspace files, including `outputs/`, are not.

### Fixed

- `apipi images check browser` mounts `/dev` before it creates `shm`
  and `pts`, so the read-only rootfs check no longer fails with
  "Read-only file system".

## [0.9.0] - 2026-09-29

### Added

- Every guest image includes `pip` and a pinned static `uv`. `pip`
  installs into the workspace user site. `uv` cache stays on `/tmp`.
- Guest image `work` adds Excel, Word, PowerPoint, PDF, CSV, and chart
  libraries. It needs sandbox size `M` or larger. It does not include
  LibreOffice or pandoc.
- Session input accepts `input_image` data URLs and a list of messages.
  A model capability registry tells Pi which models accept images.
- Agents and sessions accept OpenAI `reasoning.effort`. It maps to Pi
  thinking and is mirrored as `apipi.thinking`.
- Operator prompt fragments can be overridden per environment type, or
  from a file. `${platform_name}` replaces the built-in name. Extend
  mode replaces only Pi's intro.
- The built-in hosted prompt now says an idle or TTL stop deletes the
  workspace, and names `inputs/` and `outputs/`. Hosted sessions get a
  capability block for image, size, RAM, vCPUs, and network. It does
  not claim a browser is available.

### Documentation

- A replacement system prompt keeps the platform blocks, instructions,
  context files, and skills. It drops Pi's tool list and all tool
  guidelines, including MCP and Playwright guidance. Context files
  (`AGENTS.md` and the other names Pi loads) are documented as a
  supported way to add prompt text.

## [0.8.0] - 2026-09-29

### Breaking

- Public session responses no longer include `environment.directory`.
  The host path stays in the store for the worker.
- TOML keys removed in 0.7.0 are now unknown settings:
  `thinking_summary`, `auto_title`, `sidekick_model`,
  `sidekick_base_url`, and `sidekick_api_key`. They were ignored in
  0.7.0.

### Added

- ApiPi-only routes are canonical under `/v1/apipi/`. The old paths
  remain as deprecated aliases and are logged once per process.
  OpenAI `container_size` (`small` / `medium` / `large`) maps to
  `sandbox_size`.
- Hosted sessions expose sandbox runtime status. Cold boot emits
  `agent.session.environment.pending` and `environment.connected`.
  Every stop emits `environment.disconnected` with a reason. GET
  session includes `environment.status` and `environment.sandbox`.
  `GET /v1/agents/environments/{id}` returns that status. Eager boot
  is off unless `APIPI_SANDBOX_EAGER_BOOT` or
  `metadata["apipi.sandbox_eager_boot"]` is set.
- `apipi images check browser` is a local developer check for the
  browser image. It is not part of CI.

### Changed

- Browser and image hints are based on the resolved computer and on
  tools that actually registered, not on sandbox size. Sessions
  without a sandbox no longer get size text.
- The browser image adds Noto CJK and emoji fonts. Size `L` defaults
  to 2 vCPUs (`APIPI_SANDBOX_L_VCPUS`).

### Removed

- Unused `sandbox_playwright_mcp` / `APIPI_SANDBOX_PLAYWRIGHT_MCP`.
  The TOML key `playwright_mcp` is ignored. The dead browser hint in
  the platform prompt is gone. Playwright guidance is added only after
  those tools register.

### Fixed

- Stdio MCP servers attach again. The Pi extension now writes
  newline-delimited JSON. It wrote `Content-Length` frames, which MCP
  servers ignore. Startup no longer waits about 5 seconds per server.
- Guests mount `/dev/shm` and `/dev/pts`.
- HTTP MCP tools reach Pi. The extension lists them from the host
  credential broker URL and registers `mcp_<server_label>_<tool>`.
  The guest still does not receive the bearer. A later list failure
  is logged and the turn continues.
- A warm sandbox attach is not blocked by another session's cold boot.
- `GET` during a remote turn no longer marks the turn interrupted while
  the worker lease is live.
- Host setup runs off the event loop.
- A pool stop sends `lease.release` so idle sessions do not keep worker
  capacity. The worker handles `lease.revoke`.

## [0.7.0] - 2026-09-28

### Breaking

- Thinking summaries and automatic session titles are removed. ApiPi
  no longer emits `agent.session.turn.thinking.summary.completed`,
  `agent.session.turn.thinking.summary.failed`, or
  `agent.session.title.updated`. Titles are a client concern.
- Settings removed: `APIPI_THINKING_SUMMARY` / `thinking_summary`,
  `APIPI_AUTO_TITLE` / `auto_title`, `APIPI_SIDEKICK_MODEL` /
  `sidekick_model`, `APIPI_SIDEKICK_BASE_URL` / `sidekick_base_url`,
  and `APIPI_SIDEKICK_API_KEY` / `sidekick_api_key`. Those environment
  variables are ignored. The same TOML keys log
  `was removed in 0.7.0 and is ignored` in this release and become an
  `unknown setting` error in the next release.
- `AuthIdentity` no longer has `thinking_summary` or `auto_title`.
  A callback dict that still includes those keys is accepted; the
  keys are ignored.
- `SessionService.create` and `post_event` no longer take
  `thinking_summary` or `auto_title`.
- `apipi.title` and `apipi.title_status` are no longer written or
  interpreted. A metadata update is a plain replace, so leaving those
  keys out removes them.

## [0.6.2] - 2026-09-28

### Added

- Model-call retry and timeout settings (`APIPI_MODEL_RETRY_ENABLED`,
  `APIPI_MODEL_MAX_RETRIES`, `APIPI_MODEL_BACKOFF_BASE_MS`,
  `APIPI_MODEL_BACKOFF_MAX_MS`, `APIPI_MODEL_TIMEOUT_MS`,
  `APIPI_MODEL_PROVIDER_RETRIES`, `APIPI_MODEL_RETRY_AFTER_MAX_MS`).
  ApiPi writes them into Pi `settings.json`. Only Pi retries. See
  [Pi](docs/config.md#pi).
- `agent.session.turn.retrying` and
  `agent.session.turn.retry.completed` while Pi waits to retry a
  model call.
- `upstream_attempts` on a failed turn, the session error, the
  `turn.failed` log, the turn log, and the usage row.
- Turn failures carry `failure_source`, a specific `code`,
  `upstream_status`, and `retryable`. `agent.session.turn.failed`,
  the turn log, and the usage event use the specific code.
  `agent.session.error` and the non-stream `502` body keep
  `model_host_error` for upstream failures in this release, and put
  the specific code in `detail_code`. Set `APIPI_ERROR_CODES=specific`
  to use the specific code on those two surfaces now. The next minor
  release will make `specific` the default and keep
  `legacy_code: model_host_error` for one release after that. See
  [failure codes](docs/errors.md).

### Changed

- A turn no longer fails on the first model error when Pi will retry.
  The failure is classified after the last attempt.
- A `504` whose body mentions a timeout is `upstream_timeout`.
- A turn that exceeds `turn_timeout` fails with `turn_timeout` instead
  of looking like a user cancel. A Pi process that dies mid-turn is
  `pi_exited`. A host Pi killed for `APIPI_PI_MEM_MIB` is `pi_memory`.
  Both still report `model_host_error` on `agent.session.error` until
  the next minor release.
- `turn.failed` and `worker.command.failed` log upstream `429` and
  caller errors at warning. Internal failures and upstream `5xx`,
  timeouts, and connection errors stay at error. A user cancel stays
  info.

## [0.6.1] - 2026-09-27

### Added

- Session lifecycle export. The pool owner emits `session.live.start`,
  `session.live.stop`, and `session.live.heartbeat` when
  `APIPI_LIFECYCLE_EXPORT_URL` or `APIPI_LIFECYCLE_SINKS` is set.
  Events carry environment, image id, version, and digest so usage can
  be split by sandbox image. Off by default. See
  [usage](docs/usage.md#session-lifecycle-export).
- Auth callbacks may return `org_id`. Session create stores it and
  returns it on the session. It does not change list or get scope.

### Changed

- `apipi_pi_kill_total` now uses `crash` when a Pi process exits by
  itself, and `drain` when a worker drain kills sessions that are not
  in a turn. Drain previously incremented `idle`.

## [0.6.0] - 2026-09-27

### Added

- Agent templates. `POST /v1/templates` stores a zip of an agent's
  configuration. Import, download, and create a new agent from that
  zip. Secrets and credential values are not included. See
  [agent templates](docs/agent-templates.md).
- Agents accept `session_defaults` (`environment` and `vault_ids`).
  Session create inherits those values unless the request overrides
  them. `inherit_agent_defaults: false` skips them.
  `metadata["apipi.sandbox_size"]` and `metadata["apipi.sandbox_image"]`
  are aliases of the defaults and stay accepted.
- `APIPI_MODEL_LIST=probe|turn|off` (default `probe`). Agent create
  and model edit check the list. Turns do not. `off` never calls
  `/models` and uses `APIPI_MODELS`.

### Fixed

- A missing `agent.model` fails the session instead of hanging.
  If the host later rejects the selected model, the turn fails with
  `model_host_error`. Worker turn tasks log that failure, including
  4xx, instead of leaving an unretrieved exception.

## [0.5.3] - 2026-09-26

### Added

- `apipi images push` uploads the newest local guest image build.
  `apipi images publish` is the same command. `--to` defaults to
  `APIPI_IMAGE_SOURCE`. An identical image already in the store is
  skipped instead of failing. `--force` uploads again.
- Guest image S3 settings `APIPI_IMAGE_S3_ENDPOINT`,
  `APIPI_IMAGE_S3_REGION`, and `APIPI_IMAGE_S3_ADDRESSING`, plus
  env-only credentials `APIPI_IMAGE_S3_ACCESS_KEY_ID`,
  `APIPI_IMAGE_S3_SECRET_ACCESS_KEY`, and `APIPI_IMAGE_S3_PROFILE`.
  Unset values fall back to `APIPI_S3_*` and the default AWS
  credential chain. The artifact store is unchanged.

### Fixed

- Pushing guest images to a missing S3 bucket fails with a clear
  error instead of starting a new index.
- S3 guest image pull streams the blob to disk. Peak RAM no longer
  grows with image size. The kernel download uses the same path.

## [0.5.2] - 2026-09-25

### Added

- Auth plugins may take an `AuthRequest` (`method`, `path`, headers)
  and a `cache_key` so one organization bearer can identify end users
  without sharing a cache entry. Session create stores `user_id` when
  the identity has one. List, get, update, delete, and resume then
  match that user. Identities without `user_id` stay tenant-scoped.

### Changed

- Agent create and update validate `metadata["apipi.sandbox_size"]` and
  `metadata["apipi.sandbox_image"]`. A bad size, an unknown image, or a
  size below the image minimum is `400`. Worker availability is still a
  session placement error. Docs now say the image selects the rootfs,
  with size `L` mapping to `browser` when the image is omitted.

### Fixed

- Browser MicroVM cold start no longer runs `npx -y @playwright/mcp@latest`.
  The browser image vendors a pinned server, and auto-inject starts it
  with `node` at a fixed path. Attach soft-fails after 15 seconds so
  the first turn is not blocked for minutes. The platform prompt does
  not name Playwright MCP tools; those names are registered only after
  attach succeeds. Worker logs include the full `extension_error`
  message. Rebuild with `apipi install --microvm --image browser`.

## [0.5.1] - 2026-09-25

### Security

- Presigned GET URLs force `Content-Disposition: attachment` with the
  original file name, including an RFC 5987 `filename*` when the name is
  not ASCII. HTML, SVG, XML, and JavaScript are signed as
  `application/octet-stream` so a browser does not render them from the
  bucket domain. Gateway `/content` routes use the same attachment
  header and send `X-Content-Type-Options: nosniff`.

### Fixed

- S3 and botocore errors during harvest, cache restore, or hosted file
  and skill setup fail the turn or environment with code
  `artifact_store` instead of an unhandled `internal` error. Gateway
  reads and uploads return `503` with that code. A missing object is
  still a normal miss.

## [0.5.0] - 2026-09-24

### Added

- Prebuilt MicroVM guest images. Recipes live in `images/`.
  `apipi images build` and `apipi images publish` write a zstd rootfs,
  a manifest, and an index to `file://` or `s3://`. `apipi images pull`
  and `apipi install --microvm` install them. `environment.sandbox_image`
  and `metadata["apipi.sandbox_image"]` choose the image. Workers
  advertise images. A missing image is `503` `image_unavailable`.
- Optional `idle_ttl` on an agent and on session create. Resolve order
  is session, then agent, then the environment-type default. `0` turns
  the timer off for that session.
- Pi compaction, thinking level, and the harness system prompt are
  written into the session agent directory before Pi starts.
  `compaction.enabled` replaces the unused `--no-auto-compact` flag.
  Session `metadata["apipi.thinking"]` and
  `metadata["apipi.system_prompt"]` override the process defaults.
- Public events `agent.session.turn.compaction.started` and
  `agent.session.turn.compaction.completed`. Missing compaction events
  do not fail the turn.
- Every MicroVM guest image built from `images/` includes `curl` and
  `git`.

### Fixed

- `apipi worker` starts the idle Pi and workspace reap loops. Split
  deploys kill idle sessions. `apipi serve --api-only` still does not.

## [0.4.0] - 2026-09-24

### Fixed

- `apipi install --microvm` installs the release Jailer, not the
  `.debug` binary from the Firecracker tarball. A missing or crashing
  Jailer is replaced on the next install without `--force`.
- The microVM guest no longer receives the worker process environment.
  Guest `.apipi/env` keeps the broker URL, a dummy `OPENAI_API_KEY`,
  that session's MCP settings, and `environment.env`. It does not hold
  `APIPI_WORKER_TOKEN`, database settings, or `OPENAI_API_KEY_OVERWRITE`.
- MicroVM TAP egress rejects private and special-use IPv4, including
  RFC1918, link-local, and `100.64.0.0/10`. The public internet stays
  open. The TAP subnet stays open so the guest can reach the host broker.

## [0.3.2] - 2026-09-22

### Added

- Pi thinking level `APIPI_PI_THINKING`. Stored thinking events carry a
  100-character preview, `duration_ms`, and `reasoning_tokens`. The full
  thinking text is not a public event.
- Optional thinking summaries through a sidekick model. The platform
  flag `APIPI_THINKING_SUMMARY` and the auth callback must both allow it.
- Optional automatic session titles in `metadata["apipi.title"]` through
  the same sidekick. `APIPI_AUTO_TITLE` and a separate auth callback
  flag must both be on.
- Reserved session metadata for extenders that drive runs:
  `apipi.actor_type`, `apipi.schedule_id`, and `apipi.source`. The
  gateway stores them and does not schedule from them.

### Fixed

- A streamed create that cannot start the first turn emits
  `agent.session.error` with a code and `agent.session.failed`, then
  the stream ends.
- Session delete stops the live guest before it removes the row. A
  worker started with sudo deletes artifact files it owns.

## [0.3.1] - 2026-09-22

### Changed

- MCP vault tokens are encrypted at rest with AES-256-GCM
  (`APIPI_VAULT_MASTER_KEY`). Unset uses a local default and logs a
  warning. `apipi migrate` rewrites leftover plaintext rows.

## [0.3.0] - 2026-09-21

### Breaking

- S3 `auto` addressing is virtual-hosted. Set `APIPI_S3_ADDRESSING=path`
  for R2 or MinIO on an IP. Presigned URLs use the same style.
- MCP tools use OpenAI nested `transport` only (`http` with
  `server_url`, `stdio` with `command` / `args`). Flat `server_url` or
  `command` on the tool object is rejected. Rewrite saved agent tool
  JSON. Stdio MCP is the same API, not an ApiPi extension.
- Worker `register` requires `run_mode`. The hub picks by that
  placement class before capacity or RAM. Agents
  `environment.type=none` is placed on `chat` workers by default
  (`APIPI_ENV_NONE_PLACEMENT`). Set `microvm` to keep a computer-only
  fleet, or `reject` to fail closed with code `placement`.
- `apipi_workers` and `apipi_worker_leases` have a `run_mode` label.

### Fixed

- Chat and `environment.type=none` write broker `models.json` even
  without a workspace, so text-only turns can reach the model. Pi
  stderr is logged. Non-stream create returns `502` with `session_id`
  when the first turn fails.
- Artifact harvest I/O errors (`PermissionError` and other `OSError`)
  fail the turn with code `artifact_store` instead of `500 internal`.
- Request-start logs use the URL path. Shutdown harvest ignores
  `CancelledError`.
- Hosted follow-up after a sandbox TTL wipe fails cleanly. The Pi
  session cache reloads for the next turn.

### Changed

- Run `apipi migrate` for schema revisions `0002` through `0010`
  (workers, leases, files, skills, Pi session URI, uploads).
- `stream: true` on session create returns SSE as soon as the session
  row exists. The first turn runs in the background.
- HTTP session routes run turns through an in-process execution
  adapter. Isolation backends stay behind that boundary.
- Concepts pages for how it fits together, isolation, and workers.
  Scale docs treat worker leases as session ownership; sticky routing
  is for combined serve and `self_hosted` sockets.
- MicroVM TAP egress is public internet by default. Guest localhost
  works. The model host is always reachable. Destination allowlist is
  optional. `tc` rate limits stay.
- Artifact harvest publishes only workspace `outputs/`.
- Model and MCP secrets stay on a host credential broker, not in the
  guest.

### Added

- Optional host Pi memory ceiling `APIPI_PI_MEM_MIB` / `[pi].mem_mib`:
  Node heap hint plus RSS kill (`reason=memory`) so one session cannot
  silently fill a chat worker.
- `apipi worker` treats SIGTERM/SIGINT as drain: heartbeat
  `"drain": true`, wait until live Pi are gone, exit 0 (timeout exits
  1). Example systemd drop-in `deploy/systemd/apipi-worker-drain.conf`.
- Host Pi Prometheus series on the worker scrape: process count, RSS,
  PSS, spawn, and kill reason (`idle` / `session` / `respawn` /
  `shutdown`).
- Host workers stamp Pi and stdio MCP with `APIPI_WORKER_PID` and reap
  leftovers from a dead worker on the next start. systemd units set
  `KillMode=control-group`.
- Presigned PUT/GET for Files, Skills, and artifact downloads when
  `APIPI_ARTIFACT_STORE=s3`. Bytes go to the bucket; complete writes
  metadata. Local store returns `presign_unsupported`.
- `/v1/chat/sessions` is a GEKI-native chat facade over the same
  session store. Clients never set or see `environment`. Chat tools
  allow function tools and HTTP MCP only (`chat_tool` on deny).
- Chat fleets operator page: `APIPI_RUN_MODE=chat` vs `microvm` on one
  API-only gateway, Agents `type=none` placement, and chat to computer
  as a new session.
- Session rows store a full `file://` or `s3://` URI for the harness
  Pi session cache.
- `APIPI_RUN_MODE=chat` is a first-class alias of isolation `none` for
  dedicated chat worker pools.
- Trusted worker WebSocket at `/internal/worker` with leases,
  heartbeats, and session ownership. This is not customer
  `self_hosted`.
- `apipi serve --api-only` skips the KVM probe. `apipi worker`
  connects outbound to the API. `check` and `install` take
  `--role api|worker|all`.
- `apipi worker` probes the sandbox before it connects. Firecracker,
  jailer, and TAP stay on the worker (or on combined `apipi serve` as
  an embedded worker). API-only hosts do not create TAP devices.
- `apipi serve --api-only` leases a worker for turns. SSE reads new
  events from the store so API nodes do not need the live Pi process.
  No worker is `429` with code `capacity`.
- Worker drain (`heartbeat` `"drain": true`), RAM-first placement,
  and Prometheus gauges for workers, leases, and assign latency.
  Expired leases fail closed and are not reassigned.
- Workers advertise `capacity` (max sessions) and `memory_mb` (RAM
  budget in MiB). The API will not lease a worker that would pass
  either cap. Among eligible workers it prefers more free RAM.
- Hosted sessions accept `environment.env` and `environment.files`
  (`type: "inline"` base64, or `type: "file_id"` from the Files API)
  under `/workspace`. Reserved env names are rejected. Values persist
  for sandbox TTL rebuild.
- Files API: `POST/GET/DELETE /v1/files` and content download. Purpose
  `user_data` or `assistants`. Bytes in the shared object store. Cap
  `APIPI_MAX_FILE_BYTES` (50 MiB).
- Skills API: `POST/GET/DELETE /v1/skills` zip upload. Attach with
  `environment.skills` `skill_reference`. Unpacks under
  `.agents/skills/`. Same 50 MiB upload cap.
- Hosted sessions accept `environment.network` (`enabled`, `disabled`,
  `restricted` with exact `allowed_domains`). Session policy cannot
  widen `[sandbox.network]`. Isolation `none` cannot enforce
  `disabled` or `restricted`.
- Session sandbox sizes `S` / `M` / `L`. `environment.sandbox_size` is
  an ApiPi extension. Stock SDKs can set `metadata["apipi.sandbox_size"]`.
  `L` boots the browser rootfs. Live guests follow the resolved size,
  not process-wide `APIPI_MICROVM_IMAGE`. Size `L` also injects Playwright
  MCP (system Chromium in the guest) unless the agent already attached
  it or `auto_playwright` is off. The platform prompt mentions the
  browser only when those tools are present.
- Rootless API Docker image and Compose service (`apipi serve
  --api-only`). Worker systemd units live in `deploy/systemd/`.
- Serve logs flush each line. A turn logs `request start`,
  `turn start`, microVM boot/vsock, and `pi prompt` while SSE is
  still open.
- MicroVM guests get virtio-rng and a host random seed so Pi is
  not stuck on `getrandom()` before TLS.
- A stuck `in_progress` session is failed or cancelled so the next
  message can run. Guest console logs are debug.
- `apipi install` asks Pi / MicroVM / both on a TTY. `--microvm`
  downloads pinned Firecracker 1.17.0 and builds guest images.
  Unset kernel and rootfs paths use those cache files when they
  exist. `apipi microvm shell` re-runs under sudo with `PATH` and
  `HOME` kept.
- `POST /v1/agents/sessions/{id}/events` accepts the OpenAI nested
  `events` envelope and the existing flat body.
- OpenAI compatibility page with comparison tables for routes, fields,
  lifecycle, and errors.
- Production observability guide. Worker Prometheus metrics and
  optional guest samples. Turn metrics and traces on the worker.
  Wait-focused spans honor `traceparent`. Structured error events
  with ids and codes.
- Optional auth `user_id` on usage events and export.
- `Gateway` handle for in-process wiring. Extending docs and a
  webpage-check example. `tenant_from_key` for callers without HTTP
  auth.
- Overridable platform prompt before agent instructions.

## [0.2.0] - 2026-09-15

### Added

- `apipi microvm shell` boots the same Firecracker guest as agent
  sessions and attaches a serial shell. TAP, NAT, `ip_forward`, and
  jailer failures name the step and say when root or `CAP_NET_ADMIN`
  is missing.
- `apipi install` installs the pinned Pi CLI. `apipi check` verifies
  requirements without binding HTTP.
- Unset `DATABASE_URL` uses SQLite at `.apipi/apipi.db`. SQLite is
  enough for one process. Postgres when the store is shared.
  `apipi migrate` applies schema before `apipi serve`.

### Changed

- `output_text.delta` is live SSE only. Reconnect and export use
  `output_text.done` and items.
- File SQLite uses WAL. Production docs list when Postgres is
  required.
- Logs are JSON lines on stderr by default. `APIPI_LOG_FORMAT=text`
  is the laptop opt-in.
- Product docs use complete sentences. README and Get started cover
  install from PyPI and from a git checkout.

## [0.1.0] - 2026-09-14

First public release. Install from PyPI as `geki-apipi`. The import
package and CLI stay `apipi`.

### Added

- OpenAI Agents API compatible gateway (`apipi serve`) with Postgres as
  the source of truth.
- Pi harness over RPC. Run modes `none` (local/dev) and `microvm`
  (Firecracker). Production uses `microvm`.
- Agents, sessions, events, turns, items, artifacts, and export.
- Tenant-scoped auth via a callback. The gateway maps the bearer to a
  tenant.
- Optional S3-compatible artifact storage (`geki-apipi[s3]`).

### Changed

- Alembic history is a single baseline revision (`0001_initial`) that
  matches the current schema. Pre-0.1.0 databases have no upgrade path
  through the old revision chain. Recreate the database and run
  `apipi migrate`, or stamp `0001_initial` if the schema already
  matches.

[Unreleased]: https://github.com/GEKI-AI/apipi/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/GEKI-AI/apipi/releases/tag/v0.3.1
[0.3.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.3.0
[0.2.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.2.0
[0.1.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.1.0
