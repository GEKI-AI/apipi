# Sandbox workers

This page is the operator reference for worker protocol v2: auth,
messages, leases, drain, and which process needs KVM. The normative
message-by-message contract is
[the worker protocol specification](worker-protocol.md). Why workers
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
| `features` | Optional list of the protocol features the worker supports (see [Features and compatibility](#features-and-compatibility)). A register without `features` is a baseline worker. |
| `capacity`, `memory_mb`, `run_mode`, `arch`, `images` | Placement advertisement, as before. `capacity` is max live sessions. `memory_mb` is the RAM budget in MiB (default `capacity ×` guest `mem_mib`). `run_mode` is the process backend (`none`, `microvm`, or a custom class). `arch` is the worker machine. `images` lists `{id, version, digest, min_size}` for guest images on this host. A v2 worker that accepts `microvm` and omits `images` is treated as having `default` and `browser`, except on aarch64, which is treated as having `default` only. |

The API answers with a `hello` frame:

| Field | What |
| --- | --- |
| `ok` | Always `true`. |
| `protocol` | Always `2`. |
| `worker_id`, `generation` | The worker id and its generation. Reconnect bumps `generation` so a split brain cannot keep both sockets. |
| `connection_id` | A short id the API gives to this socket. Both processes write it on every log line of the connection, so you can follow one socket across the API and worker logs. A worker that does not see it still runs. |
| `lease_ttl_seconds` | The lease TTL the API enforces (`APIPI_WORKER_LEASE_TTL` on the API). The worker uses it to judge its own heartbeat gaps and ignores any local setting. |
| `heartbeat_seconds` | How often the worker must send `heartbeat`: a third of the lease TTL, at most 10 seconds. The worker sends it on its own timer. A `hello` without a positive value for this field or for `lease_ttl_seconds` makes the worker stop with an error. |
| `sessions` | `{session_id: last_seq}`: the persisted sequence per running session. The worker replays everything after that seq. `last_seq` is the `sessions.worker_seq` cursor that ingest advances with every batch, so a reconnect resumes exactly where the API persisted. |
| `store_check` | Only with `APIPI_ARTIFACT_STORE=local`: `{marker, nonce}`. The API writes `marker` into the shared store root containing `nonce`; the worker must read it back and answer with `store.proof`. Without the same filesystem the register is rejected with `filesystem store requires a shared path`. |
| `revoke` | Sessions the worker claimed that hold no matching lease here (`[{session_id, lease_id}]`, each sent as `lease.revoke`). The worker tears those guests down. |
| `ttl` | `{session_id: {idle_ttl_seconds, env_type, idle_since_epoch}}`: the effective reaper TTL plus the idle baseline per reported session, so a restarted worker learns idle TTLs without reading the database. |
| `features` | The protocol features the API supports (see [Features and compatibility](#features-and-compatibility)). A `hello` without `features` comes from a baseline API. |

A rejection is an object `{"type": "error", "ok": false, "error":
"<short text>", "code": "<close reason>"}`, sent before the socket
closes. `code` equals the WebSocket close reason. A first message that
is not `register` is rejected with the text `register required` and
the code `register_required`. A bad register is rejected with the text
`invalid register` and the code `invalid_register`. If no first
message arrives within 15 seconds, the API sends the reject with the
code `register_timeout` and closes the socket with code `1008` and
reason `register_timeout`. The worker treats `register_timeout` as
retryable. Rejections are logged on the API and counted in
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
| `lease.ack` | `id` (command id), `lease_id` | Command was received, and the worker sends it as soon as it has the command. It does not say the command finished. Retransmits of the same id are safe. A worker does not ack a command with an `op` it does not know. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. The worker sends it only after the API acked every envelope the worker buffered for that session (see [Release order](#release-order)). |
| `store.proof` | `marker`, `nonce` | Proof the worker sees the shared store root (filesystem store only). The worker reads the `hello` `store_check` marker file and echoes its nonce. A wrong proof closes the socket with `shared_store_required`. |
| `inventory` | `sessions: [{session_id, lease_id, last_seq}]` | The worker live set, sent on hello (as `running`), again once the outbox of a reconnect is acked (2 to 30 seconds after connecting), and every 60s after, from a timer of its own. Drives reconciliation and the lifecycle heartbeat (see [Inventory](#inventory)). |
| `sandbox.seen` | `session_ids` | Live sandbox ids, about every 5s. The API applies the same `touch_seen` update the worker used to write itself; ids not leased to this connection are ignored. |
| `search.request` | `request_id`, `session_id`, `turn_id`, `query`, `max_results` (nullable) | One `web_search` call from the Pi tool, forwarded by the session broker. A synchronous request, not an envelope. See [Search requests](#search-requests). |
| envelope (`v: 2`) | `session_id`, `turn_id`, `seq`, `type`, `payload` | Ephemeral deltas (`delta.text`, `delta.reasoning`; see [Live deltas](#live-deltas)) and durable envelopes below. Artifact and file bytes never travel here, only ids, paths, sizes, and checksums. |

The v2 envelope (`{v: 2, session_id, turn_id | null, seq, type,
payload}`) and its message schemas are defined in the
`src/apipi/protocol/` package, which the API and the worker share.
The normative contract is [the worker protocol
specification](worker-protocol.md), with JSON Schema under
`docs/worker-protocol/schema/` and golden transcripts under
`tests/fixtures/worker-protocol/`.
Every message on the socket, in both directions, is built from one of
those models and parsed through one (see [Code layout](#code-layout)).
Durable envelopes are batched per connection (about 50ms or
`APIPI_WORKER_INGEST_BATCH_SIZE` messages), applied in one
transaction per batch, and acked after commit; the API then publishes
an `EventBus` wake per stored event so SSE needs no polling.

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `protocol`, `worker_id`, `generation`, `connection_id`, `lease_ttl_seconds`, `heartbeat_seconds`, `sessions`, `store_check`, `revoke`, `ttl`, `features` | Register succeeded. `store_check` is present only for the filesystem store. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.continue`, `turn.cancel`, `session.stop`, or `sandbox.boot`. The `id` is the idempotency key: the worker acks a retransmit but never dispatches it twice, so a duplicate `turn.start` cannot start a second turn. `turn.start`, `turn.continue`, and `sandbox.boot` carry `payload.last_seq`, the session sequence cursor (see [Sequence on a new lease](#sequence-on-a-new-lease)). `turn.start` carries input images in `payload.parts` as store references (`file_id`, `object_id`, `url` or `local_path`, `mime_type`, `size_bytes`), never as bytes, and only to a worker that listed the feature `image_refs`. In a session without a computer it carries `input_file` parts the same way (`type: "file"`, plus `filename` and `model_input` `text` or `image`), only to a worker that listed the feature `file_refs`. In a session with a computer an `input_file` part has `model_input` `workspace` and a `path`, and the context lists every attachment of the session in `session_files`; the API sends both only to a worker that listed the feature `session_files`. |
| `artifact.presign.reply` | `session_id`, `request_id`, `ok`, `unchanged`, `upload_id`, `artifact_id`, `url`, `headers`, `expires_at`, `path`, `object_id`, `file_id`, `code`, `message` | Answer to one durable `artifact.presign` envelope. S3 carries a short-lived presigned PUT URL bound to a key under the session prefix (artifacts and Pi sessions) or under the files prefix (`input_image`, with `file_id` for the item part; only workers without the feature `image_refs` send it); the filesystem store carries `path`, the store-root relative path the worker must write, and no URL. When the latest stored bytes already match the presigned digest the reply carries `unchanged` instead (no URL, no path, no `upload_id`) and the worker skips the upload. `expires_at` is an RFC 3339 UTC time with `Z` and milliseconds, for example `2026-01-02T03:04:05.678Z`. Quota failures arrive as `ok: false` with today's store codes (`artifact_store`, `artifact_too_large`, `workspace_too_large`, `payload_too_large` for oversize input images). |
| `search.reply` | `session_id`, `request_id`, `ok`, `results`, `code`, `message` | Answer to one `search.request`. `results` is a list of `title`, `url`, `snippet`, and `published_date` (nullable), the same for every provider. On failure `ok` is false, `code` is one of `search_denied`, `search_unavailable`, `search_timeout`, `search_failed`, or `invalid_request`, and `message` is a short text that is safe to show the model. |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| `inventory.reply` | `revoke: [lease.revoke]`, `ttl: {session_id: {...}}` | Answer to `inventory` (and part of `hello`): sessions to tear down plus reaper TTLs. A revoke entry for an on-disk workspace the worker holds no lease for has no `lease_id`. |
| `error` | `ok: false`, `error`, `code` | Auth or register failed, then the socket closes. `code` equals the close reason (see [Handshake](#handshake)). |

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
the turn context uses for cold restore; input images from workers
without the feature `image_refs` create file rows with the returned
`file_id`. A `completed` with a foreign `upload_id`,
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
MiB). One session may use at most half of each bound, so a noisy
session or a long API outage fails that session's turn with
`worker_outbox_full` and leaves the other sessions on the worker
alone. A small emergency budget still reports the failure itself. The
worker does not pause Pi when the outbox fills up: the turn fails.

The worker checks the size of every envelope before it buffers it. An
envelope over 1,048,576 bytes (1 MiB, `MAX_MESSAGE_BYTES`, measured as
the bytes of the UTF-8 JSON text frame that goes on the wire) can never be sent, so the worker fails the turn
with `worker_message_too_large` and logs `worker.outbox.oversize`,
instead of buffering something the API would reject or that would
close the socket.

The outbox keeps a "sent" mark per session. The pump sends each
envelope once per connection, so a new envelope never makes the worker
send the whole backlog again. After a reconnect the mark starts over
and everything that is still unacked is sent once more, in order.
`apipi_worker_replayed_total` counts those real resends. The API
ingests idempotently, so a resend is safe.

An empty buffer of a session that ended is removed when the lease is
released, and so are the command dedupe entries of that session. The
worker remembers only the last sequence number of the most recent
4,096 ended sessions, so a later append continues the numbering.

### Disk spool

A bounded disk spool (`APIPI_WORKER_OUTBOX_DIR`) keeps a copy of the
buffered envelopes so they survive a worker restart. It is one
append-only JSONL file per session. An append writes one line and
returns, so a killed worker process loses nothing: the operating
system already has the line. Acks do not touch the file. Once a second
the worker fsyncs the files it appended to (group commit), in a thread
and not on the event loop, and it fsyncs once more at shutdown. A host
crash or power loss can therefore lose at most the last second of
envelopes. The same pass compacts a file in a thread when at least 512
of its lines are acked and the acked lines are at least as many as the
live ones, and it deletes the file as soon as the session has nothing
unacked. A torn last line, such as one from a kill during a write, is
skipped when the file is read again.

On start the worker reads the spool, logs one `worker.spool.recovered`
line (sessions, envelopes, bytes, and skipped lines), and sends the
envelopes after the next `hello`. After a restart the worker no
longer holds the leases, so the API may have orphaned them already.
The API then rejects the envelopes as `not_leased`, acks past them,
and the worker drops them. The spool exists so results are not lost
while a lease is still valid, for example after a quick restart.

## Monitoring

Every worker connection has metrics on both sides and log lines that
carry `worker_id` and `connection_id`. The series, the log events, and
the suggested alerts are in [observability](observability.md#worker-socket-metrics)
and in the event table of [usage](usage.md#logs). The most useful
signals are `apipi_worker_outbox_oldest_seconds` on the worker,
`apipi_worker_reconnects_total` by `reason`,
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
| `apipi.workerhub` | The API side of the socket: `WorkerHub` and its leases, command building and the command queue, register and heartbeat handling, delta checks, inventory reconcile, and `RemoteExecution`. The socket route is `apipi.api.workers`, and ingest is `apipi.services.ingest`. |
| `apipi.worker` | The worker process: the socket client (`run_worker`), command dispatch, the Pi runtime, `LocalExecution`, the outbox, `OutboxSink`, `OutboxLifecycleReporter`, artifact uploads, the Pi harness and isolation backends under `apipi.worker.pi`, and the microVM egress gateway under `apipi.worker.egress`. |
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
or is older than 30 seconds. A delta for a turn whose
`output_text.done` or terminal turn event already committed is
dropped without a database read: ingest records those turns in
memory when it stores the events of this connection, so the check
costs nothing per delta. The final item is the source of truth, and
reconnect and export skip deltas. After a reconnect the record of
finished turns starts empty, so a straggling delta for a turn that
finished before the reconnect can be published once more; it is
ephemeral and the final item replaces it.
Delta envelopes carry their own per-session `seq` counter, which the
API never checks. The durable `seq` starts at 1.
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
and installs skills from it. Input images in `turn.start` use the same
references and are fetched at turn start too.

Commands with a context are validated before send and on receipt:
file bytes are rejected, and a command frame over 262,144 bytes
(256 KiB, `MAX_COMMAND_BYTES`, measured as the bytes of the UTF-8 JSON
text frame) is rejected with `payload_too_large`. Images are references,
so only text, instructions, tools, and metadata count toward that limit. Credentials in the context never appear in logs:
the worker command log carries only a secret-free summary (operation,
environment type, model, MCP labels, file and skill counts).

## Replay

Losing the socket does not abort a turn. A session is orphaned only
after the lease TTL, and the turn is not moved to another worker.

After the socket comes back, the worker adopts the cursors in
`hello` and sends every envelope that is still unacked once
(see [Messages](#messages)). Commands the API sent but the worker did
not ack are sent again by the API when the worker reconnects and every
5 seconds while it is connected, never after the lease ended (see
[Commands](#commands)). The worker's dedupe tells such a retransmit
from a new command. A command that failed in dispatch is
not recorded as done, so its retransmit runs again.

## Commands

The API keeps every command it sent until the worker acks it with
`lease.ack`. The commands are kept per lease, in send order, in a small
queue (at most 16 per lease), so a `turn.cancel` never replaces a
`turn.start` that the worker has not acked yet. An ack removes only the
command it names. A command is sent again in three cases:

- When the worker reconnects. The API sends every unacked command of
  every lease of that worker again, in order. This includes a lease the
  worker did not claim in `register`, because the worker may never have
  read the command that granted it: the command is written to a socket
  that dies before the worker reads it. Such a lease stays attached, and
  the command arrives on the new socket. Without that, the lease would
  stay alive on heartbeats and the next turn of the session would be
  refused as `capacity`.
- On a timer while the worker is connected. A command that is still
  unacked after 5 seconds is sent again every 5 seconds.
- Never after the lease ended (release, expiry, or revoke).

A command that is still unacked after the lease TTL has failed. The API
logs `worker.command.expired` (`error_code` `worker_command_timeout`),
counts it in `apipi_worker_commands_total{result="expired"}`, clears the
lease, stores an `agent.session.error` with code `worker_command_timeout`,
fails the turn that was in progress, and sends `lease.revoke` to the
worker. A worker never acks a command whose `op` it does not know, so
such a command ends the same way, and the failure is visible. The worker
dedupes by command id, so every retransmit is safe.

The queue lives in the memory of the API replica that holds the worker
socket. If that process dies, its unacked commands are gone, and the
leases they granted end by expiry. The queue is the one place that
sends commands to a worker, so a command from another replica joins it
by the same call.

## Features and compatibility

A rolling upgrade has old and new peers on the two sides of one socket,
so the protocol has three rules.

1. **A new field is always allowed.** A receiver ignores the fields it
   does not know in envelope payloads, control messages, command
   payloads, and the command context. It counts each one in
   `apipi_worker_protocol_total{event="unknown_field"}` and logs
   `worker.protocol.unknown_field`, rate limited per message model. The
   tests parse strictly, so a typo in a sender of this repository still
   fails there.
2. **A new message type or command `op` needs a feature.** A peer sends
   it only when the other side listed the feature in `register.features`
   or `hello.features`. A receiver that still gets an unknown `type` or
   `op` logs `worker.protocol.unknown_type` or `unknown_op`, counts it
   (`unknown_type`, `unknown_op`), and never acks it as done: the worker
   does not send `lease.ack` for an unknown `op`, so the API sends it
   again and finally fails it as described in [Commands](#commands).
3. **Removing a field or changing its meaning needs a new protocol
   version.** A `register` with another `protocol` is rejected as
   `unsupported_protocol`.

A peer that sends no `features` is a baseline peer. The baseline is the
protocol as it was before features existed, so old peers keep working.

| Feature | What it adds | In the baseline |
| --- | --- | --- |
| `search` | `search.request` and `search.reply` | Yes |
| `presign` | `artifact.presign`, `artifact.completed`, and `artifact.presign.reply` | Yes |
| `lease_cursor` | `payload.last_seq` in `turn.start`, `turn.continue`, and `sandbox.boot` | Yes |
| `session_stopped` | The worker acks `session.stop` on receipt, and the durable `session.stopped` envelope is the completion that the API waits for | No |
| `image_refs` | `turn.start` carries input images as store references in `parts`, and the worker no longer uploads them as `input_image` | No |
| `file_refs` | `turn.start` carries the `input_file` parts of a session without a computer as store references in `parts` | No |
| `session_files` | `turn.start` carries the `input_file` parts of a session with a computer as workspace files, and the context carries `session_files` | No |

The worker does not wire web search when the API did not list `search`,
and it fails an upload with a clear error, instead of sending an
envelope, when the API did not list `presign`. The API refuses a command
op that needs a feature the worker did not list, and a `turn.start` with
images for a worker that did not list `image_refs`, with files for a
worker that did not list `file_refs`, or with attachments of a session
with a computer for a worker that did not list `session_files`
(`501`, `unsupported_op`). Placement prefers a worker on the same
replica that lists `image_refs` for a turn with images, `file_refs` for
a turn with files, and `session_files` for a turn with attachments. A worker on another replica is
not filtered, because its features are known only to the replica that
holds its socket.

Upgrade the API first and the workers after it. Roll back in the reverse
order: the workers first, then the API. A worker with `image_refs`
rejects the inline image parts that an older API sends. The `hello` of the API
must carry `lease_ttl_seconds` and `heartbeat_seconds`: a `hello`
without them still stops the worker with an error, because no later
version may leave them out and a worker cannot guess a safe heartbeat.

Upgrade the API first and the workers after it. A worker from before
this change rejects a command context with a field it does not know and
fails the turn with `invalid_request`, and the API of an older release
dropped an envelope payload with an unknown field. Both ignore unknown
fields from this version on, so a release may add a field to a
context or a payload only after every worker and every API runs this
version or newer.

## Delivery guarantees

Every kind of message has one guarantee, one party that retries, and a
known set of failures that lose it.

| Message | Guarantee | Who retries | What is lost, and when |
| --- | --- | --- | --- |
| Durable envelope (worker to API) | Applied once, or rejected for a permanent reason. Never acked past a temporary error. | The worker keeps it in the outbox and sends it again after every reconnect until the cumulative `ack`. The API retries a batch that failed for a temporary reason three times, then closes the socket (`ingest_failed`) so that the worker replays. | A permanent reject is acked past: the lease is not the worker's (`not_leased`), the turn does not match, the envelope is over 1,048,576 bytes, the payload is invalid, the event or type is unknown. The reject is logged and counted. After a worker host crash the spool can miss the last second of envelopes. An envelope of a lease that already ended is rejected as `not_leased`. See [A worker restart](#a-worker-restart). |
| Temporary ingest error | Not acked. The session is not acked past the last applied envelope. | The API, then the worker by replay. | Nothing. Deadlocks, lock and statement timeouts, connection resets, and object-store timeouts or throttling (also on `artifact.completed`) count as temporary. They show as `apipi_worker_ingest_total{result="transient_error"}`. |
| Ephemeral delta | At most once. | Nobody. | A delta is dropped when the lane is full, over the rate limit, after the turn ended, or when the socket drops. The final item replaces it. |
| Command (API to worker) | At least once, in order per lease. The worker runs it once (dedupe by `id`). | The API: on reconnect, every 5 seconds while connected. | A command that is not acked within the lease TTL fails visibly (see [Commands](#commands)). An API restart loses the queue of that process, and the lease then ends by expiry. |
| `session.stop` | Acked on receipt. Completion is the durable `session.stopped`. | As a command. | If `session.stopped` does not arrive within 15 seconds, the API drops the lease anyway and logs it. |
| `artifact.presign` and its reply | Always gets its reply. The reply is stored with the upload slot (by `request_id`), and a replay of the envelope gets the same reply. The API sends the reply before the ack of the envelope. | The worker, by replay of the envelope. | Nothing, except that the upload fails when the API answers with an error (quota, store). A temporary store error is not answered and not acked: it is tried again. |
| `search.request` and `search.reply` | One reply, or an error to the model. | Nobody. | A request is lost with the socket and the tool call fails (`search_unavailable` or `search_timeout`). |
| `lease.release` | Sent after the outbox was acked. | The worker retries after a reconnect. The API retries a failed handler three times. | If it never arrives, the lease expires after the TTL. |
| `lease.revoke` | Sent when the API drops a lease. | The next `hello` and `inventory.reply` list the revokes again. | A revoke written to a dead socket is not repeated until the next inventory (at most 60 seconds). |
| `heartbeat`, `inventory`, `sandbox.seen` | Periodic. | The next one. | Any single one, when the socket drops. |

The unit of every size limit is a byte of the UTF-8 JSON text frame
that goes on the wire. `MAX_MESSAGE_BYTES` is 1,048,576 (1 MiB) for one
envelope, and `MAX_COMMAND_BYTES` is 262,144 (256 KiB) for one command
frame, including its context. The worker checks the envelope size before
it appends to the outbox and fails the turn with
`worker_message_too_large`. The API checks a command before it sends it
and answers `413` with `payload_too_large`. A full outbox fails the turn
with `worker_outbox_full`.

### A worker restart

A worker process that restarts has no memory of its leases, so its
`register` claims no sessions, and its Pi processes and guests are gone.
The spool still holds the envelopes the API did not ack. When a worker
registers with an empty claim, the API keeps the leases that the row
still assigns to that worker and does not orphan them yet. The worker
replays its spool into those leases, and the API applies it. The first
`inventory` of the worker no longer lists those sessions. The worker
sends it once its outbox is fully acked, after 2 seconds at the earliest
and after 30 seconds at the latest, so the API then orphans them (`worker_orphaned`), fails
their turns, and clears the leases.

What is still lost, exactly:

- Everything that was not durable on the worker disk: deltas, the last
  second of envelopes after a host crash, and anything the worker had
  not yet appended.
- Spooled envelopes of a lease that ended before the worker came back
  and replayed: the reaper expired the lease because the worker was away
  longer than the lease TTL, or the lease was released. The API rejects
  them as `not_leased` and acks past them. The turn was already failed
  with `worker_lease_expired`, so its `usage` and later artifacts are
  not recorded.
- Envelopes of a worker that registers with a non-empty claim of other
  sessions: those sessions that it does not claim are orphaned at once,
  as before.
- The turn itself. A guest or Pi that died with the worker cannot
  continue, so the turn fails and the client sees the error.

The egress gateway certificate authority lives only in worker memory,
so a restart creates a new one. No guest outlives the authority it
trusts, because the restart ended those guests. The authority is valid
for one year, and nothing is stored or rotated.

## Keepalive and reconnect

The worker sends a WebSocket ping every 5 seconds and closes the
connection when no pong arrives within 10 seconds, so a half-open
socket is noticed within 15 seconds, well inside the default 30 second
lease TTL. Pings are only answered while the worker reads its socket.
For that reason the receive loop never waits: it parses a message and
hands the work to a task. A `session.stop`, a `lease.revoke`, a revoke
in an `inventory.reply`, a command, and the guest teardown and artifact
harvest behind them all run as tasks, so heartbeats, acks, presign
replies, and pongs keep flowing while they wait for a reply. Commands
and teardowns for the same session still run in order.

The worker waits at most 15 seconds for `hello` after it sends
`register`. If the API accepted the socket but does not answer, the
worker closes it and reconnects (`hello_timeout`). A first frame that
is not a usable `hello` also leads to a reconnect, and so does the
rejection `register_timeout`. Only another explicit rejection stops
the process with a clear message:
`unauthorized`, `revoked`, `unsupported_protocol`, `token_bound`,
`register_required`, `invalid_register`, and `shared_store_required`.
A `hello` without positive `heartbeat_seconds` and
`lease_ttl_seconds` also stops the worker.

After a lost connection the worker waits a random time before it
dials again (exponential backoff with full jitter): up to 0.5 seconds
after the first failure, then up to 1, 2, 4, 8, and at most 10
seconds. The random delay spreads the workers out after an API
restart, so they do not all reconnect and replay together. The attempt
counter starts over after a connection that stayed up for at least 30
seconds. The TLS context, with the client certificate for mTLS, is
built again for every attempt, so a rotated certificate is used
without a restart; a certificate that cannot be read during a
reconnect is retried with the same backoff.

A malformed frame (not JSON, not an object, or not a known message) is
logged as a rate limited warning (`worker.message.invalid`), counted in
`apipi_worker_messages_total{type="unknown"}`, and skipped. It does not
drop the connection. A command that raises while it runs is answered
with an `error` envelope (code `internal`, shown as an
`agent.session.error` event) and is not recorded as done. The worker
tracks a lease only for a command it accepted: a `turn.start` that the
worker rejects (wrong run mode or missing image) releases its lease
again after the failure is acked, and a `turn.cancel` for a session
the worker does not hold does not create a lease.

When the worker lets a lease go while no socket is open (an idle Pi
exits, for example), it remembers the release and sends `lease.release`
after the next `hello`, once the API acked the envelopes of that
session. Without that the claim in the next `register` would be missing
a lease the API still holds, and the API would orphan an idle session.
Every background task of the worker has a done callback that logs an
exception, and every loop catches an error per round and goes on (see
`apipi_background_loop_errors_total`).

## Artifacts

Artifact, input-image, workspace-file, skill, and Pi session bytes always
go through the configured store; the socket carries only control and
metadata messages. The gateway stores input images as files before the
turn, and the worker reads them through the image references of
`turn.start`, so it does not upload them. The worker holds no object-store credentials
and performs no artifact or file database writes: `LocalExecution` runs
with no blobs or objects, and `OutboxSink` uploads through
`artifact.presign` (outbox) -> reply -> PUT (S3, plain HTTPS with no
credentials) or shared-root write (filesystem, to the reply `path`) ->
`artifact.completed` (outbox) for the `artifact` and `pi_session`
kinds, including the killed-process harvest. The API still accepts the
`input_image` kind from workers without the feature `image_refs`. Quota
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
the rows. The API reads and hashes the object before it opens the
transaction that holds the session row lock, and it hashes in chunks
in a thread, so a large artifact or a slow store never blocks other
sockets or holds a database connection. The same holds for the store
reads of `artifact.presign` (the used bytes and the digest of the
latest stored file), and for the filesystem store, whose reads run
in a thread too.

A reconnect may go to any replica. The worker sends its running
sessions with their `last_seq` in `register`; the API answers with
the persisted `last_seq` per session in `hello`; the worker
replays everything after that seq. The reconnect also renews the
reattached leases, so running turns stay alive and the lease moves
to the new replica. Duplicates are no-ops: the
`worker_ingest` ledger claims each `(session_id, worker_seq)` inside
the batch transaction, so replays apply exactly once. Unacked
commands are retransmitted when the worker reconnects and every 5
seconds while it is connected, with the same command `id`, and the
worker keeps recent ids per
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
release started. `session.stop` follows the same order, but the
worker acks the command as soon as it has it (when both sides list the
`session_stopped` feature, see [Features and
compatibility](#features-and-compatibility)). The completion of a stop
is the durable `session.stopped` envelope: the API waits until ingest
applied it (up to 15 seconds, then it logs `worker.session.stop_timeout`)
and only then drops the lease. A baseline peer keeps the old rule: the
worker acks the command after `session.stopped` was acked, and the API
drops the lease on that ack. The wait has a limit (`RELEASE_FLUSH_TIMEOUT`, 10 seconds). When it runs
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
worker sends `heartbeat` every `heartbeat_seconds` (from `hello`)
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

## How the API serves a socket

Each worker socket runs a few tasks on the API replica that holds it,
so slow work on one kind of message never delays the others.

| Task | What it does |
| --- | --- |
| Receive loop | Reads a frame, parses it, and hands it to one of the lanes below. It does no database or store work. |
| Writer | The only place that writes to the socket. Every frame goes through its bounded queue of 1024 frames: replies, acks, commands from HTTP requests, and `lease.revoke` from the reaper. A frame that is not written within 10 seconds, or a full queue, closes the connection with the reason `write_timeout`. |
| Control lane | Handles `heartbeat`, `lease.ack`, `lease.release`, `inventory`, `sandbox.seen`, and `store.proof` in order. Ingest, presign, and artifact checks never wait in front of it, so heartbeats keep the leases alive while a large artifact is verified. |
| Ingest lane | Applies durable envelopes in batches, in order. Presign and `artifact.completed` read the object store before the batch transaction opens. A `lease.release` for a session with envelopes still waiting goes through this lane, so it never overtakes them. |
| Delta lane | Validates and publishes live deltas. When 1024 deltas wait, further deltas are dropped and counted as `delta.queue_full`. |
| Search tasks | One task per `search.request`, at most 32 at a time. |

If the ingest lane is full (2048 messages), the receive loop waits for
it, which also delays the control lane. This only happens when a
worker sends faster than the API can store.

The API sends nothing to a worker before `hello`. The `hello` frame is
the first frame in the writer queue, and the connection becomes
visible to placement, commands, and the reaper only after that, so no
command, revoke, or ack can reach a worker that has not seen its
`hello`. Because of that a new connection is not picked for a session
for the few milliseconds of its handshake.

A second socket of the same worker replaces the first on the same
replica: the older socket is closed with the reason `takeover`, and
its cleanup no longer clears the `api_instance_id` that the new socket
set. `generation` is checked: every register bumps it, and a heartbeat
from a connection with an older generation neither renews leases nor
touches the worker row. The API closes such a socket with the reason
`takeover`. This covers the case where an old socket on another
replica is still open after the worker reconnected.

## What closes a connection and what does not

These close the socket, and the reason is what
`apipi_worker_disconnects_total{reason}` and the `worker.disconnected`
log line show:

| Reason | Close code | When |
| --- | --- | --- |
| `clean` | `1000`, `1001` | The worker closed the socket. |
| `error` | other | The peer vanished, a frame was larger than the server limit (`1009`), or a task of the connection failed unexpectedly. |
| `ping_timeout` | `1011` from the server | The server's keepalive ping got no answer. |
| `write_timeout` | `1011` | A frame was not written within 10 seconds, or the writer queue was full. The worker reconnects and replays. |
| `takeover` | `1000` | The same worker connected again, or a heartbeat came from a superseded generation. |
| `revoked` | `1008` | The worker token was revoked. The API checks it on every heartbeat. |
| `protocol_violation` | `1008` | The shared store proof failed. A bad `register`, a wrong protocol, or a bad token is rejected before `hello` with its own reason (see Handshake). |
| `ingest_failed` | `1011` | One ingest batch still failed after three attempts. Nothing was acked, so the worker reconnects and replays from the persisted cursor. |

These do not close the socket:

- A database or store error while handling one message. It is logged as
  `worker.message.failed` (rate limited per message type) and counted as
  `message_failed`. A `lease.release` is retried three times with short
  pauses. A `heartbeat`, `inventory`, `sandbox.seen`, or delta that
  fails is skipped, because the worker sends the next one on its own
  timer. An ingest batch is retried three times before the connection
  is closed as `ingest_failed`. If a step after the commit fails (the
  renewal, the publish on the event bus, a lifecycle export, or the blob
  wipe), the acks are already sent and the next steps still run.
- A frame that is binary, not valid JSON, or not a JSON object. It is
  skipped, logged as `worker.frame.invalid`, and counted as
  `frame_invalid`. A message with a known `type` and invalid fields is
  skipped, logged as `worker.message.invalid`, and counted as
  `message_invalid`. An unknown `type` is logged as
  `worker.protocol.unknown_type` and counted as `unknown_type`. A
  durable envelope that fails validation is rejected and acked past, as
  before.
- A heartbeat with an invalid optional field (`capacity`, `memory_mb`,
  `run_mode`, `images`). The field is ignored and logged as
  `worker.heartbeat.field_ignored`, and the leases are still extended.
- A failure of the lease reaper. The loop logs it, counts it in
  `apipi_background_loop_errors_total{loop="lease_reaper"}`, and runs
  again one second later.

The API sets the WebSocket frame limit of uvicorn (`ws_max_size`) to
4 MiB. The 1,048,576 byte limit for one durable envelope is checked
after parsing and acked past, so the frame limit only has to be
larger than any envelope a worker may send. A frame over 4 MiB closes
the socket with code `1009`.

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

`workers.api_instance_id` is the instance id of the API process that
currently holds that worker's WebSocket. Register and heartbeat write
it. Detach clears it only if it still matches this process. A request
can land on any replica: if the worker's socket is on another one, the
command is forwarded to it, as the next section describes. SSE and
session create stay store-backed on any replica.

## Commands across API replicas

A worker holds its socket on one replica, but a request can reach any
replica. You do not have to point each worker at one API or stick
`/internal/worker` to one API. `turn.start`, `turn.continue`,
`turn.cancel`, `session.stop`, `sandbox.boot`, and `lease.revoke` all
work from any replica, and so do placement and delete. The design is in
[ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md#routing-commands-across-api-replicas).

Every API process has an instance id: `APIPI_INSTANCE_ID` (or the host
name) plus a random suffix, so a restarted process is a new instance.
When the replica that took the request has no socket for the worker,
it does this:

1. It stores one row in `worker_forwards`: the command and its
   arguments, without the turn context, and the id of the replica that
   holds the socket. The row id is the command id. An image part of
   `turn.start` keeps only its `file_id`, and a file part its `file_id`
   and `filename` (and the workspace `path` of a session with a
   computer), never a presigned URL, a store path, or an object key.
2. It sends that replica a small `forward` message with the row id,
   over the event bus. On Postgres this is `NOTIFY` on a channel of its
   own for that replica. The body never travels on the bus, because a
   command can be up to 262,144 bytes and `NOTIFY` carries 8000.
3. The replica that holds the socket claims the row, builds the turn
   context itself from the database, the vault, and the object store,
   checks each image and file `file_id` for the tenant again and builds
   its reference (the `session_files` of the context come from the
   database there too), and sends the command through its own writer and queue. From there it
   is an ordinary command: it is retransmitted, acked, and expires with
   the lease like a local one.
4. It writes the outcome to the row and sends a `forward_result`
   message back. The requesting replica returns the same result a local
   send gives: sent, acked (for the callers that wait for an ack), or
   an error. A `session.stop` returns after the worker reported
   `session.stopped`, the same as a local stop.
5. The requesting replica deletes the row.

The row holds the text, image file ids, and tool output of the turn until the
command is sent, and never the context, the vault headers, or the model
key. It is deleted as soon as the result is read. A row that a crashed
replica leaves behind is purged after 10 minutes.

A lost notification does not lose the command. The row is durable, the
requesting replica sends the notification again while it waits, and
every replica also reads its pending rows every
`APIPI_EVENT_BUS_FALLBACK_POLL` (3 seconds by default). A row is claimed
with a conditional update, so a repeated notification or poll sends the
command once, and the command id is the same on every retry.

The request bearer is never forwarded, because the gateway never writes
it to Postgres. The row keeps the identity of the request (`key_id`,
`user_id`, `org_id`, with the tenant in the row), and the replica that
holds the socket builds the context and resolves the model key from that
identity: `OPENAI_API_KEY_OVERWRITE`, then the `model_credential`
callback (see [auth](auth.md#model-credential)). Without either, the
forward fails at once with `503` `model_key_unavailable`, and the turn
does not start.

The key in `context.model.api_key` applies at the start of every turn,
not only when Pi starts. In `none` and `microvm` mode Pi never holds the
key: it calls the credential broker on the host with a placeholder, and
the worker sets the broker's key in place before the turn. A short-lived
or rotated credential therefore works for a Pi that stays up across
turns, and Pi is not restarted for it. A custom run mode that does not
use the broker gets the key only when Pi starts.

What it costs: for each forwarded command, one insert, one claim, one
status update, and one delete on a small table, plus two notifications.
Local sends are unchanged. The first byte of a forwarded turn is
about one notification and a few queries later than a local one.

How it fails:

| Case | Result |
| --- | --- |
| The replica holding the socket stopped heartbeating (its worker's `last_seen` is older than `APIPI_WORKER_LEASE_TTL`) | At once: `503` `worker_unreachable`. The worker reconnects to another replica and the lease is taken over as usual. |
| The replica is slow or gone inside that window and never claims the row | After 10 seconds: `504` `forward_timeout`. |
| The worker disconnected from that replica meanwhile | `503` `worker_unreachable`. |
| The replica rejected the command (`capacity`, `unsupported_op`, `image_unavailable`, `payload_too_large`) | The same error and status a local send gives. |

`turn.cancel` is never a silent success. If the session holds a live
lease and the cancel could not be delivered, the request fails with
`503` `worker_unreachable` or `504` `forward_timeout`. A session with no
lease has nothing to cancel and the request answers as before. Delete
sends `session.stop` the same way; when that cannot be delivered the API
logs `worker.stop.undelivered`, clears the lease, and deletes the
session, and the worker drops the guest at its next inventory.

Forwarding needs the Postgres event bus. `InMemoryEventBus` (the default
on SQLite) keeps messages in one process, so forwarding and fleet
placement are off, and a second API process on the same database is not
supported. See [production](production.md#several-api-replicas).

## Placement

`WorkerHub.pick` matches the **accepts set** before capacity or RAM.
`placement_for` returns `none` for `environment.type=none` and
`microvm` for every other type. A session is assigned only to a
connected worker whose accepts set contains that kind, using the
existing least-loaded logic (most free RAM, then fewer leases).
There is no fallback to another kind.

### Placement across replicas

Placement looks at every connected worker in the fleet, not only the
ones whose socket is on the replica that took the request. It reads the
workers on its own replica from their connections. It reads the others
from the `workers` table: every register and heartbeat writes the
worker's `capacity`, `memory_mb`, `accepts`, image ids, `arch`, and
drain flag next to `last_seen`, and the leases and their memory come
from the leased session rows. Only workers heard from within
`APIPI_WORKER_LEASE_TTL` count. The ordering is the same (most free RAM,
then fewer leases).

The data about a worker on another replica is at most one heartbeat old
(at most 10 seconds, a third of the lease TTL), so a worker that just
filled up or started draining can still be picked. The replica that
holds its socket then checks capacity again when it grants the lease,
and answers `capacity` if there is no room, so a stale read never
overcommits a worker. The lease grant and the first command are
forwarded as one request, so the grant, the send, and the bookkeeping
happen on the replica that holds the socket.

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
then reports `session.stopped` as a durable envelope, after the API
acked everything the worker buffered for the session (see [Release
order](#release-order)). The worker acks the command itself as soon as it
receives it. The API drops the lease only after it applied
`session.stopped`. A delete does not wait for idle TTL.

`SIGTERM` or `SIGINT` on `apipi worker` sends that drain heartbeat,
kills idle Pi (sessions not in a turn), waits until no live Pi remain
and the API acked every envelope in the outbox, then exits 0. In-flight
turns finish first. The idle kills run while the socket is open, so
their artifact harvest can upload. If live Pi or unacked envelopes
remain after `--drain-timeout` (default idle TTL, 15 minutes), the
worker kills the rest while the socket is still open (the harvest gets
at most 30 seconds), logs `worker.drain.finished` with `error_code`
`drain_timeout` and the unacked counts, and the process exits 1, and
systemd may then SIGKILL the cgroup. The timeout is enforced while the
worker is disconnected too: it keeps trying to reconnect, so it can
still deliver the outbox, but it gives up at the deadline. While no
socket is open a killed session does not harvest its files; the worker
logs `worker.harvest.skipped` instead of waiting 60 seconds for a reply
that cannot arrive. `systemctl stop` and
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
expire mid-turn, even when its socket is busy with envelopes. The
reaper clears the rows and stores the error events in one transaction.
Only after that commit does it write the log lines, forget the
in-memory lease, and send `lease.revoke` to the worker if it is
connected to this replica. Each revoke has a 5 second timeout and goes
through the connection's writer, so a stuck socket cannot hold the row
locks or stop the loop. A failed revoke is logged as
`worker.lease.revoke_failed`.

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
