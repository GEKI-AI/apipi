# Sandbox workers

This page is the operator reference for worker protocol v2: auth,
messages, leases, drain, and which process needs KVM. Why workers
exist and how a turn moves is in [Workers](worker-concepts.md). The
architecture choice is in
[ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md).
Isolation of Pi is in [isolation](isolation.md).

Trusted ApiPi workers host Firecracker. They are **not** customer
external computers. `self_hosted` is currently not supported
(see [environments](environments.md)). Workers use `/internal/worker`,
a per-worker token, and the v2 messages below.

Firecracker, jailer, TAP, and the guest live on the **worker**.
`apipi serve` is always the API: it never probes `/dev/kvm` and never
creates a TAP device. Production is the API plus one or more
`apipi worker` hosts. For a laptop or one box, `apipi dev` starts the
API and one worker as two child processes. Fleet layouts (one worker type that does both,
separate `microvm` and `none` workers, or `none`-only) are below.

`apipi worker` reads its token from `APIPI_WORKER_TOKEN_FILE` and
probes the configured run mode before it connects. If
`APIPI_RUN_MODE=microvm` cannot start, the worker exits. It does not
fall back to `none`.

`apipi serve` runs every turn on a leased worker. The API persists
events from the store and streams SSE without Pi on that node. If no
worker can take a lease, the turn returns `429` with code `capacity`.

Start everything through the ApiPi CLI:

```
apipi serve
apipi dev
apipi workers token create --name worker-1
APIPI_WORKER_TOKEN_FILE=/run/apipi/worker.token APIPI_API_URL=http://api.example:8000 apipi worker
apipi check --role api
apipi check --role worker
apipi install --role api
apipi install --role worker
```

`apipi dev` is the single-host path for local development. It runs
`apipi migrate`, creates or reuses a dev worker token in
`.apipi/dev-worker-token`, and starts `apipi serve` and `apipi worker`
as two child processes. `apipi worker` is the sandbox process. It is not a tenant computer.

## Auth

The worker opens an outbound WebSocket to `/internal/worker` and
sends `Authorization: Bearer <token>`. Each worker has its own token:

```
apipi workers token create --name worker-1   # prints the secret once
apipi workers token list
apipi workers token revoke <id-or-name>
```

Only the SHA-256 hash of a token is stored in the API database
(`worker_tokens`: id, name, hash, creation time, last use,
revocation). Every secret starts with `apipi_wk_`, so the public API
recognises a worker bearer by its prefix and rejects it without a
database lookup. The secret is shown once at creation and never again.
Write it to a file on the worker host and point the worker at it:

```
APIPI_WORKER_TOKEN_FILE=/run/apipi/worker.token
```

The file may end with a newline; surrounding whitespace is trimmed.
`apipi worker` fails at startup when the setting is unset, or when the
file is missing, unreadable, or empty. The token is never read from a
plain environment value. There is no shared worker secret: the old
`APIPI_WORKER_TOKEN` was removed, and setting it fails API and worker
startup with a message that points at
`apipi workers token create`.

A token is bound to one `worker_id` on first register, or declared at
creation with `--worker-id`. A register without `id` is assigned the
bound `worker_id` by the API, which is how a restarted `apipi worker`
(which sends no `id` and keeps no stable id of its own) keeps working.
A register with a different explicit `id` is rejected with
`token_bound`, whether or not the bound worker is connected. Keep several active tokens per
worker so rotation needs no downtime: create the new token, roll it
out to the worker, then revoke the old one. A revoked token closes
live sockets on the next heartbeat, and new registers with it are
rejected.

A worker token is valid only on `/internal/worker`. It is rejected
with `401` on every other route. Worker tokens are operator secrets,
not tenant bearers. Do not put them in a browser.

## Transport security

Worker tokens are bearer secrets, so the socket needs TLS outside
local development. `apipi worker` fails at startup when its API URL
is a non-loopback plain URL: use `https://` (or `wss://`) for any
API host that is not loopback. Loopback `http://` URLs stay allowed
for local development. The message types do not change with TLS.

For mutual TLS, give the worker a client certificate and key, and
point it at the private CA that signed the API server certificate
when the system trust store does not cover it:

```
APIPI_WORKER_CLIENT_CERT=/run/apipi/worker.crt
APIPI_WORKER_CLIENT_KEY=/run/apipi/worker.key
APIPI_WORKER_SERVER_CA=/run/apipi/api-ca.crt
```

The certificate and key must be set together; `apipi worker` fails
at startup when only one is set or when a file is missing or
unreadable. Terminate TLS (and verify the client certificate
against your CA) on the reverse proxy or load balancer in front of
the API. See [production](production.md#worker-transport-security).

## Handshake

The first worker message must be `register` with `protocol: 2`:

| Field | What |
| --- | --- |
| `protocol` | Must be `2`. Anything else closes the socket with code `1008` and reason `unsupported_protocol`. There is no fallback for old workers. |
| `id` | Optional worker UUID. When omitted, the API assigns the token's bound `worker_id` (or mints and binds one on first register). |
| `capabilities` | Free-form object, reserved for later steps. |
| `accepts` | List of session kinds from `none`, `microvm`. What this worker runs. See [Placement](#placement). |
| `running` | Sessions this worker still holds: `[{session_id, lease_id, last_seq}]`. `last_seq` continues from the API's value on reconnect. |
| `version` | Optional. The ApiPi version of the worker. The API shows it in the `version` label of `apipi_worker_info`. |
| `capacity`, `memory_mb`, `run_mode`, `arch`, `images` | Placement advertisement, as before. `capacity` is max live sessions. `memory_mb` is the RAM budget in MiB (default `capacity ×` guest `mem_mib`). `run_mode` is the process backend (`none`, `microvm`, or a custom class). `arch` is the worker machine. `images` lists `{id, version, digest, min_size}` for guest images on this host. A v2 worker that accepts `microvm` and omits `images` is treated as having `default` and `browser`, except on aarch64, which is treated as having `default` only. |

The API answers with `hello.reply`:

| Field | What |
| --- | --- |
| `protocol` | Always `2`. |
| `worker_id`, `generation` | The worker id and its generation. Reconnect bumps `generation` so a split brain cannot keep both sockets. |
| `connection_id` | A short id the API gives to this socket. Both processes write it on every log line of the connection, so you can follow one socket across the API and worker logs. A worker that does not see it still runs. |
| `lease_ttl_seconds` | The lease TTL the API enforces (`APIPI_WORKER_LEASE_TTL` on the API). The worker uses it to judge its own heartbeat gaps and ignores any local setting. |
| `heartbeat_seconds` | How often the worker must send `heartbeat`: a third of the lease TTL, at most 10 seconds. The worker sends it on its own timer. A `hello` without a positive value for this field or for `lease_ttl_seconds` makes the worker stop with an error. |
| `sessions` | `{session_id: last_seq}`: the persisted sequence per running session. The worker replays everything after that seq. `last_seq` is the `sessions.worker_seq` cursor that ingest advances with every batch, so a reconnect resumes exactly where the API persisted. |
| `store_check` | Only with `APIPI_ARTIFACT_STORE=local`: `{marker, nonce}`. The API writes `marker` into the shared store root containing `nonce`; the worker must read it back and answer with `store.proof`. Without the same filesystem the register is rejected with `filesystem store requires a shared path`. |
| `revoke` | Sessions the worker claimed that hold no matching lease here (`[{session_id, lease_id}]`, each sent as `lease.revoke`). The worker tears those guests down. |
| `ttl` | `{session_id: {idle_ttl_seconds, env_type, idle_since_epoch}}`: the effective reaper TTL plus the idle baseline per reported session, so a restarted worker learns idle TTLs without reading the database. |

A first message that is not `register` is rejected with
`register required`. A bad register is rejected with
`invalid register`. Rejections are logged on the API and counted in
`apipi_worker_connects_total{result}` (`unsupported_protocol`,
`invalid_register`, `unauthorized`, `revoked`, `token_bound`). A
durable envelope that fails ingest validation (not leased to this
worker, wrong turn, oversize, unknown event) is dropped, logged as
`worker.event.rejected`, and counted as `envelope_rejected`; the
cumulative ack still moves past it so the worker does not resend it.

## Messages

JSON objects. `register` is the only pre-handshake message.

Worker to API:

| `type` | Fields | What |
| --- | --- | --- |
| `register` | See [Handshake](#handshake) | Create or reconnect the worker. |
| `heartbeat` | `capacity` (optional), `memory_mb` (optional), `run_mode` (optional), `accepts` (optional), `arch` (optional), `image_store_version` (optional), `drain` (optional bool), `images` (optional list) | Refresh `last_seen` and renew every lease of the worker. Sent every `heartbeat_seconds` from a timer of its own, so a busy socket does not delay it. May update caps, advertised `run_mode` and accepts set, architecture, drain posture, and the image list. |
| `lease.ack` | `id` (command id), `lease_id` | Command was received. Retransmits of the same id are safe. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. The worker sends it only after the API acked every envelope the worker buffered for that session (see [Release order](#release-order)). |
| `store.proof` | `marker`, `nonce` | Proof the worker sees the shared store root (filesystem store only). The worker reads the `hello` `store_check` marker file and echoes its nonce. A wrong proof closes the socket with `shared_store_required`. |
| `inventory` | `sessions: [{session_id, lease_id, last_seq}]` | The worker live set, sent on hello (as `running`) and every 60s after, from a timer of its own. Drives reconciliation and the lifecycle heartbeat (see [Inventory](#inventory)). |
| `sandbox.seen` | `session_ids` | Live sandbox ids, about every 5s. The API applies the same `touch_seen` update the worker used to write itself; ids not leased to this connection are ignored. |
| `event` | `lease_id`, `event_type`, `data` | Persist a public session event. The worker must hold that lease. Unknown event types are ignored. |
| `search.request` | `request_id`, `session_id`, `turn_id`, `query`, `max_results` (nullable) | One `web_search` call from the Pi tool, forwarded by the session broker. A synchronous request, not an envelope. See [Search requests](#search-requests). |
| envelope (`v: 2`) | `session_id`, `turn_id`, `seq`, `type`, `payload` | Ephemeral deltas (`delta.text`, `delta.reasoning`; see [Live deltas](#live-deltas)) and durable envelopes below. Artifact and file bytes never travel here, only ids, paths, sizes, and checksums. |

The v2 envelope (`{v: 2, session_id, turn_id | null, seq, type,
payload}`) and its message schemas are defined in the
`src/apipi/protocol/` package, which the API and the worker share.
Every message on the socket, in both directions, is built from one of
those models and parsed through one (see [Code layout](#code-layout)).
Durable envelopes are batched per connection (about 50ms or
`APIPI_WORKER_INGEST_BATCH_SIZE` messages), applied in one
transaction per batch, and acked after commit; the API then publishes
an `EventBus` wake per stored event so SSE needs no polling.

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `protocol`, `worker_id`, `generation`, `lease_ttl_seconds`, `heartbeat_seconds`, `sessions`, `store_check` | Register succeeded. `store_check` is present only for the filesystem store. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.continue`, `turn.cancel`, `session.stop`, or `sandbox.boot`. The `id` is the idempotency key: the worker acks a retransmit but never dispatches it twice, so a duplicate `turn.start` cannot start a second turn. `turn.start`, `turn.continue`, and `sandbox.boot` carry `payload.last_seq`, the session sequence cursor (see [Sequence on a new lease](#sequence-on-a-new-lease)). |
| `artifact.presign.reply` | `session_id`, `request_id`, `ok`, `unchanged`, `upload_id`, `artifact_id`, `url`, `headers`, `expires_at`, `path`, `object_id`, `file_id`, `code`, `message` | Answer to one durable `artifact.presign` envelope. S3 carries a short-lived presigned PUT URL bound to a key under the session prefix (artifacts and Pi sessions) or under the files prefix (`input_image`, with `file_id` for the item part); the filesystem store carries `path`, the store-root relative path the worker must write, and no URL. When the latest stored bytes already match the presigned digest the reply carries `unchanged` instead (no URL, no path, no `upload_id`) and the worker skips the upload. Quota failures arrive as `ok: false` with today's store codes (`artifact_store`, `artifact_too_large`, `workspace_too_large`, `payload_too_large` for oversize input images). |
| `search.reply` | `session_id`, `request_id`, `ok`, `results`, `code`, `message` | Answer to one `search.request`. `results` is a list of `title`, `url`, `snippet`, and `published_date` (nullable), the same for every provider. On failure `ok` is false, `code` is one of `search_denied`, `search_unavailable`, `search_timeout`, `search_failed`, or `invalid_request`, and `message` is a short text that is safe to show the model. |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| `inventory.reply` | `revoke: [lease.revoke]`, `ttl: {session_id: {...}}` | Answer to `inventory` (and part of `hello.reply`): sessions to tear down plus reaper TTLs. A revoke entry for an on-disk workspace the worker holds no lease for has no `lease_id`. |
| error object | `ok: false`, `error` | Auth or register failed, then the socket closes. |

Message classes:

| Class | Types | Delivery |
| --- | --- | --- |
| Durable | `item.added`, `item.done`, `turn.status`, `usage`, `event`, `session.status`, `artifact.presign`, `artifact.completed`, `session.stopped`, `workspace.reaped`, `lifecycle.start`, `lifecycle.stop`, `error`, `sandbox.status` | Kept in the worker outbox until the cumulative `ack{last_seq}`. Ingested idempotently. |
| Ephemeral | `delta.text`, `delta.reasoning` | At-most-once, never persisted, never acked. The final item is the source of truth. |
| Synchronous request | `search.request`, `search.reply` | One request and one reply keyed by `request_id`. Not durable, not in the outbox, not acked, never replayed. |

`item.added` carries the full item (the API creates the row; the
runtime reports the added and done public events as `event`
envelopes). `turn.status` carries the turn row transition (`started`
creates the row, the rest close it). `usage` carries the turn usage
record with the in-memory tool and MCP tallies. `event` carries any
other public event with its data. `session.status` carries the
session status change. `error` carries a worker-reported error.
`artifact.completed` and `sandbox.status` are accepted on the wire
and applied by ingest. `artifact.presign` reserves the upload slot and
returns its reply on the same socket; `artifact.completed` carries the
API-issued `upload_id` with the observed size and checksum (plus `path`
for the filesystem store) and verifies the object (S3 `HEAD` size and
checksum, or the shared-root file size and checksum) before writing
rows. Artifacts are stored with the presigned `artifact_id`, so the
object and the row stay bound; Pi sessions update the session pointer
the turn context uses for cold restore; input images create file rows
with the returned `file_id`. A `completed` with a foreign `upload_id`,
a path outside the expected key, or a checksum or size mismatch is
rejected.
`sandbox.status` carries a sandbox phase (`starting`, `ready`,
`stopped`) with the same fields the pool used to pass to
`record_transition`; the API applies that transition, so the
`environment.*` events look the same to clients as before.
`session.stopped` is the durable receipt for the wipe after
`session.stop`, and `workspace.reaped` the receipt for an idle
workspace wipe. Only `session.stopped` deletes the session blobs
on ingest (the worker already wiped its local workspace directory);
`workspace.reaped` is a receipt only, so a session leased again
before its receipt lands keeps its artifacts.
`lifecycle.start` and `lifecycle.stop` carry one live session each;
the API persists and exports them (see [Lifecycle
state](#lifecycle-export)).

The outbox is bounded (`APIPI_WORKER_OUTBOX_MAX_MESSAGES`, default
10,000 messages, and `APIPI_WORKER_OUTBOX_MAX_BYTES`, default 64
MiB). When it is full the worker fails the turn with
`worker_outbox_full` (a small emergency budget still reports that
failure itself). Envelopes are capped at 1 MiB. A bounded disk spool
(`APIPI_WORKER_OUTBOX_DIR`) keeps a write-through copy of buffered
envelopes so they survive a worker restart.

## Monitoring

Every worker connection has metrics on both sides and log lines that
carry `worker_id` and `connection_id`. The series, the log events, and
the suggested alerts are in [observability](observability.md#worker-socket-metrics)
and in the event table of [usage](usage.md#logs). The most useful
signals are `apipi_worker_outbox_oldest_seconds` on the worker,
`apipi_worker_heartbeat_gap_seconds` and `apipi_worker_handle_seconds`
on the API, and the `worker.disconnected` line, whose `reason` tells you
why a socket closed.

## Code layout

The wire contract and the two sides of the socket live in separate
packages, so the contract has one home and a worker can run without the
API code.

| Package | What it holds |
| --- | --- |
| `apipi.protocol` | Every wire model, both directions: the handshake, control messages, envelopes and their payloads, commands with one payload model per `op`, the command context, replies, close reasons, and constants such as `PROTOCOL_VERSION` and the size limits. It imports only pydantic and the standard library. The base classes are named by role: `ControlMessage` for messages outside the envelope stream, `EnvelopePayload` for envelope payloads, `CommandPayload` for command payloads, and `ContextPart` for sections of the command context. `parse_worker_message` and `parse_api_message` turn one incoming frame into its model, and every model writes its frame with `to_wire()`. |
| `apipi.workerhub` | The API side of the socket: `WorkerHub` and its leases, command building and the unacked command table, register and heartbeat handling, delta checks, inventory reconcile, and `RemoteExecution`. The socket route is `apipi.api.workers`, and ingest is `apipi.services.ingest`. |
| `apipi.worker` | The worker process: the socket client (`run_worker`), command dispatch, the Pi runtime, `LocalExecution`, the outbox, `OutboxSink`, `OutboxLifecycleReporter`, artifact uploads, and the Pi harness and isolation backends under `apipi.worker.pi`. |
| `apipi.common` | Code both sides use: logging, metrics, tracing, failure codes, the sandbox and agent metadata rules, the in-process event bus, and object store paths. It imports no FastAPI, SQLAlchemy, or store code. |

`tests/unit/test_import_boundary.py` enforces the split. It starts a
worker in a subprocess and checks that SQLAlchemy, asyncpg, FastAPI,
`apipi.store`, `apipi.api`, `apipi.gateway`, `apipi.services`, and
`apipi.workerhub` are not loaded. It also scans the sources: the API
packages import nothing from `apipi.worker`, `apipi.common`,
`apipi.protocol`, and `apipi.worker` import nothing from the API
packages, and `apipi.protocol` imports only pydantic and the standard
library.

## Search requests

The built-in [`web_search` tool](tools.md#web-search) works through the
worker socket, because only the API holds the search provider key. The
Pi tool calls the session broker, the broker hands the call to the
worker, and the worker sends `search.request` with the `session_id`,
the `turn_id`, the query, and the optional `max_results`. The
message carries no provider name, no key, and no domain list. The API
reads `filters.allowed_domains` from the session's effective agent
definition, so the worker cannot widen it.

The API treats the worker as untrusted. On every request it checks
that the session belongs to the tenant of the connection's lease, that
the turn is running, that the effective agent tools include
`web_search`, and that the search resolver still allows search for the
session's tenant and subject. It then calls the provider, records
usage (see [usage](usage.md#search)), and sends `search.reply`. The
reply is sent before the tool result reaches Pi, so the counts are
stored before the turn's `usage` envelope is ingested.

Search is not durable. It does not use the outbox or the ingest batch.
Before it sends a request, the worker waits until the API has
acknowledged every envelope the session produced so far, for at most
five seconds, so the API already knows that the turn is running. If the
acknowledgements do not arrive in that time, the model gets a tool
error and no request is sent. The
worker keeps one waiter per `request_id` and waits for the reply
for at most 30 seconds. These are the failure rules:

| Case | What the model sees |
| --- | --- |
| The provider fails or times out | A tool error. The reply has `ok: false` with `search_unavailable`, `search_timeout`, or `search_failed`. |
| Search is not allowed (no tool on the agent, no provider, a resolver denial, or the turn is not running) | A tool error. The reply has `ok: false` with `search_denied`. |
| The request is malformed or the query is empty or too long | A tool error. The reply has `invalid_request`. |
| No reply in 30 seconds | A tool error from the worker. |
| The socket drops | Every waiter fails at once with a connection error, which is a tool error. |

None of these fails the turn. The model may call the tool again. After
a reconnect the worker does not replay old requests, and the worker
ignores a late reply for a waiter that no longer exists. A search that was already
charged by the provider before the failure is still counted in usage.

## Live deltas

The worker coalesces model text fragments over about 40ms per
session and sends each batch as one ephemeral `delta.text` envelope.
Batches over 4000 characters are split so every envelope stays well
under the `NOTIFY` payload limit. The API checks that the session is
leased to the sending worker (otherwise the delta is rejected and
logged), applies a 32 KiB size cap and a per-session rate budget
(100 deltas per second; over-budget deltas are dropped and counted),
and publishes accepted deltas as `live` bus messages without writing
to the store. The lease check reads the socket's in-memory lease
state and only re-reads the lease row when that state cannot answer
or is older than 30 seconds; a delta for a turn whose
`output_text.done` or terminal turn event already committed is
dropped after reading only the events stored since the last check
(turns already known done need no read at all): the final item is
the source of truth, and reconnect and export skip deltas.
Per-session delta state is dropped when the lease ends or the
connection closes. `delta.reasoning` envelopes are accepted but
never fanned out.
Rejections and drops are counted in
`apipi_worker_protocol_total{event}` (`delta.accepted`,
`delta.rejected`, `delta.dropped_done`, `delta.rate_limited`,
`delta.oversize`, `delta.reasoning_dropped`, `envelope_rejected`).
## Command context

`turn.start`, `turn.continue`, and `sandbox.boot` carry a `context`
object in the command payload. The API builds it from the database,
the vault, and the object store; the worker holds it in memory only
and never logs it. The schemas live in
`src/apipi/protocol/context.py`, and the builder in
`src/apipi/services/turn_context.py`. The model key travels only in
`context.model.api_key`. The command payload has no key of its own.

| Field | What |
| --- | --- |
| `session` | The resolved environment, metadata, `required_actions`, identity (`user_id`, `org_id`, `key_id`), status, and the effective idle TTL in seconds (resolved on the API, so the worker reaper needs no database read). |
| `agent` | The resolved definition: model, instructions, function tools, metadata, `builtin_tools`, `codemode`, the thinking level, and `web_search`. `web_search` is only a boolean: it says the Pi tool is loaded for this turn. The API sets it from the agent tools and the search resolver. The context never carries the provider name, the key, or the domain list. |
| `mcp` | The resolved HTTP MCP servers (`server_label`, `server_url`, vault-applied `headers`, `allowed_tools`). Rebuilt from the database and the vault on every turn, so a follow-up on another API replica works. |
| `model` | The model base URL override (if any) and the model key. |
| `files`, `skills` | References only, never bytes. |
| `pi_session` | The cold-restore reference for the Pi session blob, if one exists. |

File bytes never travel in the command. With `APIPI_ARTIFACT_STORE=s3`
each file, skill, and Pi session blob becomes a presigned GET URL with
a short TTL. With the filesystem store each reference becomes a path
relative to the shared store root (`APIPI_LOCAL_STORE_DIR`, default `.apipi/store`), which the worker
reads directly; the API and the worker must see the same filesystem.
The worker fetches the bytes at turn start, provisions the workspace,
and installs skills from it.

Commands with a context are validated before send and on receipt:
file bytes are rejected, and payloads over 256 KiB are rejected with
`payload_too_large`. Credentials in the context never appear in logs:
the worker command log carries only a secret-free summary (operation,
environment type, model, MCP labels, file and skill counts).

## Replay

Losing the socket does not abort a turn. A session is orphaned only
after the lease TTL, and the turn is not moved to another worker.

## Artifacts

Artifact, input-image, workspace-file, skill, and Pi session bytes always
go through the configured store; the socket carries only control and
metadata messages. The worker holds no object-store credentials
and performs no artifact or file database writes: `LocalExecution` runs
with no blobs or objects, and `OutboxSink` uploads through
`artifact.presign` (outbox) -> reply -> PUT (S3, plain HTTPS with no
credentials) or shared-root write (filesystem, to the reply `path`) ->
`artifact.completed` (outbox) for `artifact`, `pi_session`, and
`input_image` kinds, including the killed-process harvest. Quota
failures raise the usual codes so the turn fails
(`artifact_store` fails the turn, other quota codes emit the session
error event). The worker uploads every changed file; the API keeps the latest
version per path. Unchanged files never leave the worker: when the
presigned digest matches the latest stored bytes for that path, the
API answers `unchanged` before checking quotas and the worker skips the PUT and the `completed` envelope, so
no new row is written. Pi sessions reuse the existing blob id, so
every save overwrites the same object instead of leaking one new
object per save. The total workspace limit (`workspace_too_large`)
is enforced on the worker from settings in both harvest paths, with
the same codes.

With `APIPI_ARTIFACT_STORE=s3` (the recommended production setup)
the worker sends durable `artifact.presign` with the session id, kind
(`artifact`, `pi_session`, or `input_image`), filename, content type,
size, and checksum; the API checks quotas (`max_workspace_bytes` and
`max_artifact_bytes`, or `max_file_bytes` for input images) before
issuing a short-lived presigned PUT URL bound to a key under the
session prefix (or the files prefix with a `file_id` for input
images); the worker uploads with a plain PUT; then it sends durable
`artifact.completed` with the `upload_id`, size, and checksum. The API
verifies the object (`HEAD` size and checksum where available),
writes the artifact, file, or Pi session pointer rows, and emits the
existing public events. Reads use the presigned GET references in the
command context, and downloads go through the existing routes. With
the filesystem store the API returns the exact store-root relative
`path` in the reply; the worker writes there and reports
`artifact.completed` with that path; the API validates the path
matches the reserved key, checks size and checksum, and then writes
the rows.

A reconnect may go to any replica. The worker sends its running
sessions with their `last_seq` in `register`; the API answers with
the persisted `last_seq` per session in `hello.reply`; the worker
replays everything after that seq. The reconnect also renews the
reattached leases, so running turns stay alive and the lease moves
to the new replica. Duplicates are no-ops: the
`worker_ingest` ledger claims each `(session_id, worker_seq)` inside
the batch transaction, so replays apply exactly once. Unacked
commands are retransmitted
with the same `command.id`, and the worker keeps recent ids per
session for its whole lifetime (not per connection, so a replay after
a reconnect is still recognised): a retransmit is acked again but
never dispatched twice, so
a duplicate `turn.start` cannot start a second turn. Ids are forgotten
when the session is torn down, stopped, or revoked.

## Sequence on a new lease

The sequence number of a session is one counter for the life of the
session, not one per lease. The API ledger holds each `(session_id,
seq)` once and never clears it, so a worker that restarted a session
at seq 1 would have its whole turn skipped as duplicates. To prevent
that, every `turn.start`, `turn.continue`, and `sandbox.boot` carries
`payload.last_seq`, the `sessions.worker_seq` cursor read after the
lease was granted. The worker calls `set_base` on its outbox with that
value before it dispatches the command, so the next envelope is
`last_seq + 1`. The call never moves the counter backwards and drops
buffered envelopes the API already holds, so it is safe on a worker
that kept the buffer of an earlier lease. A command without a valid
`last_seq` is dispatched anyway and logged as
`worker.command.cursor_missing`.

Only the worker that holds the session's lease can move the cursor.
Envelopes from a worker that no longer holds the lease are rejected
and acked, but they do not advance `sessions.worker_seq`, so they
cannot make the next lease holder skip its own envelopes.

## Release order

Envelopes and control messages share one socket, but the outbox pump
and the control messages are separate writers, so a `lease.release`
could overtake envelopes still waiting in the outbox. The API clears
the lease when it handles the release, and ingest rejects envelopes of
a session without a lease as `not_leased`. That lost the `lifecycle.stop` billing
envelope, the final `sandbox.status`, harvested `artifact.completed`
envelopes, and `session.stopped`.

The worker therefore sends `lease.release` only after the API acked
every envelope the worker had buffered for the session when the
release started. The same rule holds for the `lease.ack` of a
`session.stop` command: it follows the ack of `session.stopped`. The
wait has a limit (`RELEASE_FLUSH_TIMEOUT`, 10 seconds). When it runs
out, for example because the socket is down, the worker releases
anyway and logs `worker.release.unflushed`. The API also ingests the
batch it still holds before it handles a `lease.release`, so envelopes
that arrived before the release are never rejected because of it.

`workspace.reaped` is sent after an idle wipe, which is after the
lease ended, so it can never be ordered before the release. It changes
nothing on the API, so the API acks it for any existing session
without a lease and without a ledger row.

## Heartbeats and lease renewal

A lease expires `APIPI_WORKER_LEASE_TTL` after its last renewal. The
worker sends `heartbeat` every `heartbeat_seconds` (from `hello.reply`)
on a timer that does not depend on the socket being quiet, and the
API renews every lease of that worker on each heartbeat. As defense in
depth the API also renews a worker's leases when the worker acks a
command or when a durable batch from it is committed, at most once per
heartbeat interval. The worker sends the inventory and runs its drain
check on their own timers as well. A worker that has
`APIPI_WORKER_LEASE_TTL` set ignores it and logs
`worker.lease_ttl.ignored`.

`apipi_worker_heartbeat_gap_seconds` records the gap between two
heartbeats on both sides, and a gap above half the lease TTL logs
`worker.heartbeat.late`. See [observability](observability.md#worker-leases-and-ingest).

## Inventory

On hello and every 60s after, the worker reports its live set
as `inventory{sessions: [{session_id, lease_id, last_seq}]}`. The API
compares it with the lease rows for that worker. Sessions leased
here but not reported are orphaned: the API records
`agent.session.error` with code `worker_orphaned` and clears the
lease, unless a command for that lease is still unacked (in flight
to the worker, which cannot have reported it yet). Sessions the worker
reports without a matching lease come
back as `lease.revoke`, and the worker tears those guests down and
drops their outbox buffers. Reported sessions that are still leased
get their effective idle TTL in the reply, which seeds the worker
reaper without a database read. A restarted worker that reports an
empty live set therefore fails its old turns once (on the API) and
relearns TTLs as new commands arrive.

The inventory also carries on-disk workspaces the worker holds no
lease for (entries without `lease_id`), so leftovers from a crash or
a stop while the worker was down are reconciled too. A released lease
only means the Pi stopped: while the session row is still alive the
API answers with its reaper TTL (plus the idle baseline, so the
reaper clock does not restart on every reply) and the worker reaper
wipes the directory when the TTL runs out. Only once the row is gone
does the API revoke, and the worker wipes the directory and reports
`workspace.reaped` as a receipt. Only `session.stopped` deletes the
session blobs.

## Leases

A lease is durable on the session row (`worker_id`, `lease_id`,
`lease_until`) and owned entirely by the API. Grant is a single
conditional `UPDATE`: it only
succeeds when there is no live lease. The replica holding the socket
renews the lease on heartbeat (and on command acks and committed ingest batches, see [Heartbeats and lease renewal](#heartbeats-and-lease-renewal)). On reconnect the new replica takes the
lease over: register already points `workers.api_instance_id` at it,
and `restore_leases` renews exactly the leases the worker still
reports with a conditional `UPDATE` matching `worker_id` and
`lease_id`, so reconnecting to another replica keeps running turns
alive without touching leases granted elsewhere. Heartbeats extend all of that
worker's leases in one statement. Commands carry `lease_id`. A worker
that does not hold that lease cannot ack, emit events, or release it.

When `lease_until` passes, API processes expire rows with
`FOR UPDATE SKIP LOCKED` so two reapers do not double-clear. The API
clears ownership, emits `agent.session.error` with code
`worker_lease_expired`, and sends `lease.revoke` if the worker is
still connected. It does not assign the session to another worker in
this version.

`workers.api_instance_id` is the `APIPI_INSTANCE_ID` of the API process
that currently holds that worker's WebSocket. Register and heartbeat
write it. Detach clears it only if it still matches this process. If
a turn needs that worker but this process has no socket, the API
returns `429` with code `capacity` and names that instance. There is
no cross-API command forwarding. Point each worker at the API that
will dispatch its turns, or stick `/internal/worker` to one API. SSE
and session create stay store-backed on any replica.

## Placement

`WorkerHub.pick` matches the **accepts set** before capacity or RAM.
`placement_for` returns `none` for `environment.type=none` and
`microvm` for every other type. A session is assigned only to a
connected worker whose accepts set contains that kind, using the
existing least-loaded logic (most free RAM, then fewer leases).
There is no fallback to another kind.

`APIPI_WORKER_ACCEPTS` (`[worker].accepts`) is a comma list from
`none`, `microvm`. One code path covers three layouts:

| `APIPI_WORKER_ACCEPTS` | Layout |
| --- | --- |
| `none,microvm` | One worker does both. `environment.type=none` runs Pi directly on the worker host, and every other type runs in a microVM. |
| `microvm` | MicroVM sessions only. |
| `none` | `type=none` sessions only. No KVM needed. |

The default follows the backend: a worker whose backend can run
microVMs accepts `none,microvm`, and a worker without a microVM
backend accepts only `none`. In practice that means
`APIPI_RUN_MODE=microvm` defaults to both and `APIPI_RUN_MODE=none`
defaults to `none`-only. Startup validation fails fast before register
when `microvm` is in the list but the microVM backend cannot run
(KVM, Firecracker, or images missing).

Run one API with the workers the fleet needs:

```
apipi serve
apipi workers token create --name none-1       # prints the secret once
apipi workers token create --name computer-1
APIPI_WORKER_ACCEPTS=none APIPI_WORKER_TOKEN_FILE=/run/apipi/none.token APIPI_API_URL=http://api.example:8000 apipi worker
APIPI_WORKER_ACCEPTS=microvm APIPI_WORKER_TOKEN_FILE=/run/apipi/computer.token APIPI_API_URL=http://api.example:8000 apipi worker
```

| Process | `APIPI_WORKER_ACCEPTS` | What it serves |
| --- | --- | --- |
| `apipi serve` | unused for Pi | HTTP, store, placement |
| `none` worker | `none` | Light Pi on the host. No Firecracker. Teardown kills the Pi process group. |
| `microvm` worker | `microvm` or `none,microvm` | One KVM guest per computer session, plus host Pi for `type=none` when both are accepted. |

Pi for `type=none` always runs directly on the worker host: no
microVM and no small guest. This is acceptable because `none`
sessions have no shell, file, or workspace tools. `type=none` allows
function tools, HTTP MCP, and `web_search` only; anything else is `400`.

No matching worker with capacity is `429` with code `capacity`.

Commands include `run_mode` in the payload carrying the required kind
(`none` or `microvm`). The worker compares that to its accepts set
and does not start Pi when it does not match.

Host Pi sizing and supervision still apply per worker. Set
`APIPI_PI_MEM_MIB` so one session cannot fill the worker. After a
worker crash, the next start reaps leftover host Pi processes from
that worker. Use `KillMode=control-group` on the systemd unit.
`systemctl restart` sends SIGTERM so the worker drains, then starts
again. Install `deploy/systemd/apipi-worker-drain.conf` so stop can
wait for live Pi to empty. Scrape `apipi_pi_processes` and
`apipi_pi_rss_bytes` on the worker when metrics are on. See
[Drain and expiry](#drain-and-expiry) and
[observability](observability.md#prometheus).

Saved agents that still carry `metadata.apipi.session_kind=chat` are
ignored for placement now. Bundle import drops that key with a
warning; exports no longer carry it. Use
`environment.type=none` for text-only sessions.

## Idle reap

Idle Pi reap and hosted workspace wipe run on the process that holds
Pi. `apipi worker` runs those loops and never reads the database:
when the worker knows the session from a command context, the reaper
uses the context's effective idle TTL measured from the last turn
activity; when it does not (for example after a worker restart), the
session waits for the API inventory reply, which carries the
effective TTL and the idle baseline per reported session. `none` use `APIPI_IDLE_TTL`.
Hosted computers use
`APIPI_SANDBOX_TTL_OPENAI_HOSTED`. A wiped workspace is reported as
a durable `workspace.reaped` envelope, which the API ingests as a
receipt only (it never deletes the session blobs). A host Pi kill increments
`apipi_pi_kill_total` with reason `idle` on the worker metrics
endpoint. A process that exits by itself is `crash`. Worker drain
uses `drain`, not `idle`.

## Drain and expiry

A heartbeat may include `"drain": true`. That worker keeps its current
leases and heartbeats them, but the scheduler does not give it new
sessions. After the placement filter, it picks among workers that are
not draining, have a free session slot, and have enough remaining
`memory_mb` for one more guest (`mem_mib` from `[sandbox.resources]`,
default 512). Among those it prefers the worker with the most free
RAM. Session count is only a filter and a tie-break.

Session delete sends `session.stop` to the worker that holds the
lease. The worker kills that guest and deletes host files it owns,
then acknowledges once the API has acked everything the worker buffered
for the session, including `session.stopped` (see [Release
order](#release-order)). The API drops the lease only after that
acknowledgement. A delete does not wait for idle TTL.

`SIGTERM` or `SIGINT` on `apipi worker` sends that drain heartbeat,
kills idle Pi (sessions not in a turn), waits until no live Pi remain,
then exits 0. In-flight turns finish first. If live Pi remain after
`--drain-timeout` (default idle TTL, 15 minutes), the process exits 1
and systemd may then SIGKILL the cgroup. `systemctl stop` and
`systemctl restart` send SIGTERM. Raise `TimeoutStopSec` so stop can
wait; the example drop-in is `deploy/systemd/apipi-worker-drain.conf`
(`TimeoutStopSec=16min`). Copy it to
`/etc/systemd/system/apipi-worker.service.d/drain.conf`. The default
unit keeps `TimeoutStopSec=15` so a Firecracker stop still fails fast
unless you install the drop-in.

When `lease_until` passes, the lease is cleared and the session gets
`worker_lease_expired`. The turn is not moved to another worker: the
guest and workspace were on the expired host. Start a new turn after
that error. Heartbeats extend `lease_until` so a live worker does not
expire mid-turn, even when its socket is busy with envelopes.

Host Pi (`none`) is a child of the worker. A graceful stop
runs pool teardown. A `kill -9` of the worker leaves those children.
The next worker start reaps leftovers stamped with a dead
`APIPI_WORKER_PID`. Set `KillMode=control-group` on the systemd unit
(`deploy/systemd/apipi-worker.service`) so `systemctl stop` kills the
cgroup. See [production](production.md#failure-and-drain).

## Lifecycle export

The API owns the lifecycle export: `apipi serve` emits it, and `apipi
worker` only reports. The worker sends session live start and stop
as durable v2 envelopes (`lifecycle.start`, `lifecycle.stop`), so
they survive disconnects and replay exactly once after reconnect,
and it sends its live set as the periodic `inventory`, from which
the API derives the heartbeat export. The worker holds no export URL
or token: `apipi worker` ignores `APIPI_LIFECYCLE_*` settings with a
startup warning. The pool reporter tags each envelope with the
`worker_id` from `hello`.
See
[session lifecycle export](usage.md#session-lifecycle-export).

## What runs where

| Process | Trust | Needs |
| --- | --- | --- |
| `apipi serve` | Operator control plane | Postgres, no KVM |
| `apipi worker` | Operator sandbox host | Its token file, outbound to the API, the model host, the image store, and MCP upstreams. KVM and Firecracker only when it accepts `microvm`. No Postgres, no object-store credentials, no search provider name or key. |

The worker holds its running sessions in memory only and makes no
database queries: turns run from the command context, results go
through the outbox, sandbox and lifecycle state go over the socket,
and the reaper learns TTLs from the context and the inventory reply.
`apipi worker` refuses to start when `DATABASE_URL` is set in its
environment or `database_url` is set in its config file: unset it on
worker hosts, since only the API connects
to Postgres. With `APIPI_ARTIFACT_STORE=s3` the worker uploads and
downloads bytes through API-issued presigned URLs and never sees
store credentials; with the filesystem store it uses the shared
store root that the API and every worker mounts at the same path.
The socket needs TLS for any non-loopback API URL (see
[Transport security](#transport-security)).
The per-worker token is an operator secret. It is not a tenant
bearer. Do not put it in the browser. The API container in Compose is
unprivileged. The worker unit is the only place that should receive
`/dev/kvm` and `CAP_NET_ADMIN`.
