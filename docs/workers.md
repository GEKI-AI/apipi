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
`apipi serve --api-only` never probes `/dev/kvm` and never creates a
TAP device. Combined `apipi serve` (no `--api-only`) is the
single-host embedded worker: the same in-process adapter as today,
for a laptop or one box. Production is API-only plus one or more
`apipi worker` hosts. Fleet layouts (one worker type that does both,
separate `microvm` and `none` workers, or `none`-only) are below.

`apipi worker` reads its token from `APIPI_WORKER_TOKEN_FILE` and
probes the configured run mode before it connects. If
`APIPI_RUN_MODE=microvm` cannot start, the worker exits. It does not
fall back to `none`.

`apipi serve --api-only` (or `APIPI_API_ONLY`) runs turns on a leased
worker. The API persists events from the store and streams SSE without
Pi on that node. If no worker can take a lease, the turn returns `429`
with code `capacity`. Combined `apipi serve` still runs turns
in-process.

Start everything through the ApiPi CLI:

```
apipi serve
apipi serve --api-only
apipi workers token create --name worker-1
APIPI_WORKER_TOKEN_FILE=/run/apipi/worker.token APIPI_API_URL=http://api.example:8000 apipi worker
apipi check --role api
apipi check --role worker
apipi install --role api
apipi install --role worker
```

Combined `apipi serve` keeps today's single-host path. `--api-only`
skips the KVM probe so the API can run without Firecracker.
`apipi worker` is the sandbox process. It is not a tenant computer.

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

This is not mTLS yet. A later change can add it without changing the
message types.

## Handshake

The first worker message must be `register` with `protocol: 2`:

| Field | What |
| --- | --- |
| `protocol` | Must be `2`. Anything else closes the socket with code `1008` and reason `unsupported_protocol`. There is no fallback for old workers. |
| `id` | Optional worker UUID. When omitted, the API assigns the token's bound `worker_id` (or mints and binds one on first register). |
| `capabilities` | Free-form object, reserved for later steps. |
| `accepts` | List of session kinds from `none`, `microvm`. What this worker runs. See [Placement](#placement). |
| `running` | Sessions this worker still holds: `[{session_id, lease_id, last_seq}]`. `last_seq` continues from the API's value on reconnect. |
| `capacity`, `memory_mb`, `run_mode`, `arch`, `images` | Placement advertisement, as before. `capacity` is max live sessions. `memory_mb` is the RAM budget in MiB (default `capacity ×` guest `mem_mib`). `run_mode` is the process backend (`none`, `microvm`, or a custom class). `arch` is the worker machine. `images` lists `{id, version, digest, min_size}` for guest images on this host. A v2 worker that accepts `microvm` and omits `images` is treated as having `default` and `browser`, except on aarch64, which is treated as having `default` only. |

The API answers with `hello.reply`:

| Field | What |
| --- | --- |
| `protocol` | Always `2`. |
| `worker_id`, `generation` | The worker id and its generation. Reconnect bumps `generation` so a split brain cannot keep both sockets. |
| `sessions` | `{session_id: last_seq}`: the persisted sequence per running session. The worker replays everything after that seq. `last_seq` is the `sessions.worker_seq` cursor that ingest advances with every batch, so a reconnect resumes exactly where the API persisted. |
| `store_check` | Only with `APIPI_ARTIFACT_STORE=local`: `{marker, nonce}`. The API writes `marker` into the shared store root containing `nonce`; the worker must read it back and answer with `store.proof`. Without the same filesystem the register is rejected with `filesystem store requires a shared path`. |

A first message that is not `register` is rejected with
`register required`. A bad register is rejected with
`invalid register`. Rejections are logged on the API and counted in
`apipi_worker_protocol_total{event}` (`unsupported_protocol`,
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
| `heartbeat` | `capacity` (optional), `memory_mb` (optional), `run_mode` (optional), `accepts` (optional), `arch` (optional), `drain` (optional bool), `images` (optional list) | Refresh `last_seen`. May update caps, advertised `run_mode` and accepts set, architecture, drain posture, and the image list. |
| `lease.ack` | `id` (command id), `lease_id` | Command was received. Retransmits of the same id are safe. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. |
| `store.proof` | `marker`, `nonce` | Proof the worker sees the shared store root (filesystem store only). The worker reads the `hello` `store_check` marker file and echoes its nonce. A wrong proof closes the socket with `shared_store_required`. |
| `event` | `lease_id`, `event_type`, `data` | Persist a public session event. The worker must hold that lease. Unknown event types are ignored. |
| envelope (`v: 2`) | `session_id`, `turn_id`, `seq`, `type`, `payload` | Ephemeral deltas (`delta.text`, `delta.reasoning`; see [Live deltas](#live-deltas)) and durable envelopes below. Artifact and file bytes never travel here, only ids, paths, sizes, and checksums. |

The v2 envelope (`{v: 2, session_id, turn_id | null, seq, type,
payload}`) and its message schemas are defined in
`src/apipi/worker/protocol.py`, which the API and the worker share.
Durable envelopes are batched per connection (about 50ms or
`APIPI_WORKER_INGEST_BATCH_SIZE` messages), applied in one
transaction per batch, and acked after commit; the API then publishes
an `EventBus` wake per stored event so SSE needs no polling.

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `protocol`, `worker_id`, `generation`, `sessions`, `store_check` | Register succeeded. `store_check` is present only for the filesystem store. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.continue`, `turn.cancel`, `session.stop`, or `sandbox.boot`. The `id` is the idempotency key. |
| `artifact.presign.reply` | `session_id`, `request_id`, `ok`, `upload_id`, `url`, `headers`, `expires_at`, `path`, `object_id`, `file_id`, `code`, `message` | Answer to one durable `artifact.presign` envelope. S3 carries a short-lived presigned PUT URL bound to a key under the session prefix (artifacts and Pi sessions) or under the files prefix (`input_image`, with `file_id` for the item part); the filesystem store carries `path`, the store-root relative path the worker must write, and no URL. Quota failures arrive as `ok: false` with today's store codes (`artifact_store`, `artifact_too_large`, `workspace_too_large`, `payload_too_large` for oversize input images). |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| error object | `ok: false`, `error` | Auth or register failed, then the socket closes. |

Message classes:

| Class | Types | Delivery |
| --- | --- | --- |
| Durable | `item.added`, `item.done`, `turn.status`, `usage`, `event`, `session.status`, `artifact.presign`, `artifact.completed`, `error`, `sandbox.status` | Kept in the worker outbox until the cumulative `ack{last_seq}`. Ingested idempotently. |
| Ephemeral | `delta.text`, `delta.reasoning` | At-most-once, never persisted, never acked. The final item is the source of truth. |

`item.added` carries the full item (the API creates the row; the
runtime reports the added and done public events as `event`
envelopes). `turn.status` carries the turn row transition (`started`
creates the row, the rest close it). `usage` carries the turn usage
record with the in-memory tool and MCP tallies. `event` carries any
other public event with its data. `session.status` carries the
session status change. `error` carries a worker-reported error.
`artifact.completed` and `sandbox.status` are accepted on the wire
but only `artifact.completed` is applied yet: ingest rejects sandbox
messages (counted, and the worker keeps them buffered) until the step
that owns them lands. `artifact.presign` reserves the upload slot and
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

The outbox is bounded (`APIPI_WORKER_OUTBOX_MAX_MESSAGES`, default
10,000 messages, and `APIPI_WORKER_OUTBOX_MAX_BYTES`, default 64
MiB). When it is full the worker fails the turn with
`worker_outbox_full` (a small emergency budget still reports that
failure itself). Envelopes are capped at 1 MiB. A bounded disk spool
(`APIPI_WORKER_OUTBOX_DIR`) keeps a write-through copy of buffered
envelopes so they survive a worker restart.

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
and never logs it. Combined serve builds the same context in-process,
so both paths run the identical turn preparation. The schemas live in
`src/apipi/worker/turn_context.py`, and the builder in
`src/apipi/services/turn_context.py`.

| Field | What |
| --- | --- |
| `session` | The resolved environment, metadata, `required_actions`, identity (`user_id`, `org_id`, `key_id`), status, and the effective idle TTL in seconds (resolved on the API, so the worker reaper needs no database read). |
| `agent` | The resolved definition: model, instructions, function tools, metadata, `builtin_tools`, `codemode`, and the thinking level. |
| `mcp` | The resolved HTTP MCP servers (`server_label`, `server_url`, vault-applied `headers`, `allowed_tools`). Rebuilt from the database and the vault on every turn, so a follow-up on another API replica works. |
| `model` | The model base URL override (if any) and the model key. |
| `files`, `skills` | References only, never bytes. |
| `pi_session` | The cold-restore reference for the Pi session blob, if one exists. |

File bytes never travel in the command. With `APIPI_ARTIFACT_STORE=s3`
each file, skill, and Pi session blob becomes a presigned GET URL with
a short TTL. With the filesystem store each reference becomes a path
relative to the shared store root (`APIPI_LOCAL_STORE_DIR`, falling back
to `APIPI_SESSIONS_DIR`), which the worker
reads directly; the API and the worker must see the same filesystem.
The worker fetches the bytes at turn start, provisions the workspace,
and installs skills exactly as combined serve does.

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
metadata messages. The split worker holds no object-store credentials
and performs no artifact or file database writes: `LocalExecution` runs
with no blobs or objects, and `OutboxSink` uploads through
`artifact.presign` (outbox) -> reply -> PUT (S3, plain HTTPS with no
credentials) or shared-root write (filesystem, to the reply `path`) ->
`artifact.completed` (outbox) for `artifact`, `pi_session`, and
`input_image` kinds, including the killed-process harvest. Combined
serve keeps today's direct path through `DirectSink`. Quota failures
raise today's codes so the turn fails the way direct writes do
(`artifact_store` fails the turn, other quota codes emit the session
error event). Split uploads every file; the API keeps the latest
version per path (no worker-side dedup in split mode).

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
replays everything after that seq. Duplicates are no-ops: the
`worker_ingest` ledger claims each `(session_id, worker_seq)` inside
the batch transaction, so replays apply exactly once. Unacked
commands are retransmitted
with the same `command.id`, so the worker must treat that id as
idempotent and never run a turn twice.

## Leases

A lease is durable on the session row (`worker_id`, `lease_id`,
`lease_until`). Grant is a single conditional `UPDATE`: it only
succeeds when there is no live lease. Heartbeats extend all of that
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

Run one API-only gateway with the workers the fleet needs:

```
apipi serve --api-only
apipi workers token create --name none-1       # prints the secret once
apipi workers token create --name computer-1
APIPI_WORKER_ACCEPTS=none APIPI_WORKER_TOKEN_FILE=/run/apipi/none.token APIPI_API_URL=http://api.example:8000 apipi worker
APIPI_WORKER_ACCEPTS=microvm APIPI_WORKER_TOKEN_FILE=/run/apipi/computer.token APIPI_API_URL=http://api.example:8000 apipi worker
```

| Process | `APIPI_WORKER_ACCEPTS` | What it serves |
| --- | --- | --- |
| `apipi serve --api-only` | unused for Pi | HTTP, store, placement |
| `none` worker | `none` | Light Pi on the host. No Firecracker. Teardown kills the Pi process group. |
| `microvm` worker | `microvm` or `none,microvm` | One KVM guest per computer session, plus host Pi for `type=none` when both are accepted. |

Pi for `type=none` always runs directly on the worker host: no
microVM and no small guest. This is acceptable because `none`
sessions have no shell, file, or workspace tools. `type=none` allows
function tools and HTTP MCP only; anything else is `400`.

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
Pi. Combined `apipi serve` starts those loops in the API process.
`apipi worker` starts the same loops. `apipi serve --api-only` does
not kill idle guests; the worker that owns the session does. When the
worker knows the session from a command context, the reaper uses the
context's effective idle TTL measured from the last turn activity, with
no database read; otherwise it resolves the TTL from the session and
agent rows as before. `none` use `APIPI_IDLE_TTL`. Hosted computers use
`APIPI_SANDBOX_TTL_OPENAI_HOSTED`. A host Pi kill increments
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
then acknowledges. The API drops the lease only after that
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
expire mid-turn.

Host Pi (`none`) is a child of the worker. A graceful stop
runs pool teardown. A `kill -9` of the worker leaves those children.
The next worker start reaps leftovers stamped with a dead
`APIPI_WORKER_PID`. Set `KillMode=control-group` on the systemd unit
(`deploy/systemd/apipi-worker.service`) so `systemctl stop` kills the
cgroup. See [production](production.md#failure-and-drain).

## Lifecycle export

Session live start, stop, and heartbeat events are emitted by the
process that owns the `PiPool`. That is `apipi worker`, or combined
`apipi serve`. `apipi serve --api-only` does not emit them and does
not relay them. The worker learns its `worker_id` from `hello` and
puts that id on each event. Embedded serve leaves `worker_id` null.
The hub does not forward lifecycle events. See
[session lifecycle export](usage.md#session-lifecycle-export).

## What runs where

| Process | Trust | Needs |
| --- | --- | --- |
| `apipi serve --api-only` | Operator control plane | Postgres, no KVM |
| `apipi worker` | Operator sandbox host | Its token file, outbound to the API, the model host, the image store, and MCP upstreams. KVM and Firecracker only when it accepts `microvm`. |
| Combined `apipi serve` | Lab / one box | Whatever the run mode needs, including KVM when `microvm` |

The worker still reads the session store today; later protocol steps
remove that access so the worker keeps only its running sessions in
memory. The per-worker token is an operator secret. It is not a tenant
bearer. Do not put it in the browser. The API container in Compose is
unprivileged. The worker unit is the only place that should receive
`/dev/kvm` and `CAP_NET_ADMIN`.
