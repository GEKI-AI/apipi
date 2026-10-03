# 0015. Worker protocol v2

Only the API writes to Postgres. A worker is a just-in-time executor:
it gets session context and credentials with each command, keeps them
in memory only, and reports results over the worker WebSocket
(`/internal/worker`, protocol v2). It holds no database credentials
and no object-store credentials. A compromised worker host then sees
only the sessions it is currently running, not the whole database.

The public HTTP API is unchanged by this decision.

The code keeps the two sides of the socket apart. Every message that
crosses the socket is a pydantic model in the `apipi.protocol`
package, which imports only pydantic and the standard library. The API
side lives in `apipi.workerhub` and the worker side in `apipi.worker`.
Neither imports the other, and what both need lives in `apipi.common`.
The model key travels only in `context.model.api_key` of a command, so
the typed and redacted context is the one place that holds it. A test
enforces the boundary, and a worker process never loads SQLAlchemy,
asyncpg, FastAPI, or the store.

## Transport

The worker dials one outbound WebSocket to `/internal/worker` per API
replica it serves. The API pushes `command` messages; the worker sends
envelopes back. There is no queue system (Celery, Temporal). The
follow-up fan-out from the API to SSE clients uses Postgres
`LISTEN/NOTIFY` behind an `EventBus` interface (a later step), which
leaves room for NATS or Redis without touching the worker wire.

A WebSocket was chosen because the API must push commands and lease
revocations to a specific worker, and the worker must stream results
back, over one long-lived connection that survives NAT and load
balancers. A queue would add an operator component and still need a
push channel for revocations and deltas.

## Envelope

Every worker to API message after the handshake is
`{v: 2, session_id, turn_id | null, seq, type, payload}`. `seq` is
monotonic per session and assigned by the worker. It continues from
the API's `last_seq` on a new lease or reconnect: a reconnect gets the
cursor in `hello.reply`, and a new lease gets it in the command that
starts the work (see Sequence on a new lease).

Message classes:

* **Durable** (`item.added`, `item.done`, `turn.status`, `usage`,
  `artifact.completed`, `error`, `sandbox.status`): the worker keeps an
  outbox until it gets a cumulative `ack{session_id, last_seq}`. The
  API ingests idempotently (`UNIQUE(session_id, seq)`,
  `ON CONFLICT DO NOTHING`), in batches, and acks after commit. The
  final item is the source of truth, never the deltas.
* **Ephemeral** (`delta.text`, `delta.reasoning`): at-most-once, never
  persisted, never acked. Deltas are coalesced before fan-out.

API to worker: `command{command_id, session_id, op, lease_id,
payload}` (idempotent by `command_id`, on the wire as `id`),
cumulative `ack{session_id, last_seq}`, `lease.revoke`, and
`hello.reply{protocol: 2, lease_ttl_seconds, heartbeat_seconds,
sessions: {id: last_seq}}`.

## Handshake and version

The first worker message must be
`register{protocol: 2, worker_id, capabilities, accepts?, running:
[{session_id, lease_id, last_seq}]}`. `accepts` is an optional list of
session kinds and is reserved for placement work; it carries no
behavior yet. The API rejects anything other than `protocol: 2` with
close code 1008 and reason `unsupported_protocol`, logs the rejection,
and counts it in `apipi_worker_protocol_total`. A hard cut is
accepted: old workers cannot connect, and there is no fallback.

The API replies with its persisted `last_seq` per running session
(`0` until sequence persistence lands). The worker replays everything
after that seq. Sequence persistence is not part of this step: until
the ingest and replay step lands, the API always sends `last_seq: 0`
as an explicit placeholder. Nothing may rely on the value yet.

## Leases, sequence, and release

Three rules keep a running turn and its results from vanishing without
an error. Each was chosen as the smallest change that holds.

**Heartbeats come from a timer.** The worker sends `heartbeat` from its
own task on a monotonic timer, not when the socket has been quiet. The
API acks every ingest batch, so a busy turn keeps the socket from ever
being quiet, and a quiet-socket heartbeat starved until the lease
expired. The inventory and the drain check run on timers too. The API
owns the numbers: `hello.reply` carries `lease_ttl_seconds` and
`heartbeat_seconds` (a third of the TTL, at most 10 seconds), and the
worker uses them. A worker-side `APIPI_WORKER_LEASE_TTL` has no effect
and logs a warning, so the two sides cannot disagree. As defense in
depth the API also renews a worker's leases on a command ack and on a
committed ingest batch, at most once per heartbeat interval. Late
heartbeats, lease events, duplicate claims, and `not_leased` rejects
are counted and logged so they are visible in production.

**The command carries the cursor.** `turn.start`, `turn.continue`, and
`sandbox.boot` carry `payload.last_seq`, the persisted
`sessions.worker_seq` read after the lease is granted. The worker calls
`outbox.set_base` before it dispatches, which never moves backwards, so
it is safe on a worker that kept an earlier buffer. The alternative was
to key the ingest ledger by `(session_id, lease_id, seq)`. That changes
the ledger, the cursor, and the meaning of `hello.sessions`, so the
cursor in the command was preferred. Only the lease holder advances
`sessions.worker_seq`: envelopes rejected from a stale worker are acked
but never move the cursor.

**Release waits for the outbox.** The worker sends `lease.release`, and
the `lease.ack` of `session.stop`, only after the API acked every
envelope buffered for that session, with a timeout (10 seconds) after
which it releases anyway and logs. This was chosen over a durable
`lease.released` envelope and over carrying a lease id in every
envelope, because both change the envelope schema and the ingest
checks, while the wait is local to the worker and uses the cumulative
ack that already exists. The API also flushes the batch it holds
before it handles a `lease.release`. `workspace.reaped` is sent after
the lease ended by design, so the API acks it for any existing session
without a lease and without a ledger row; it changes nothing.
`session.stop` runs as its own task, because waiting for acks inside
the receive loop would block the acks it waits for.

## Semantics

* Losing the socket does not abort a turn. A session is orphaned only
  after the lease TTL, and the turn is not moved to another worker:
  the guest and workspace were on the expired host.
* A reconnect may go to any replica. It replays after `hello.reply`.
* The outbox is bounded (`OUTBOX_BOUND`, 10,000 messages, and a byte
  bound). One session may use at most half of each bound. When a bound
  is hit the turn fails with `worker_outbox_full`. The worker does not
  pause Pi: pausing the output stream would need a backpressure path
  from the outbox into the Pi RPC reader, and a turn that cannot report
  its results is better failed with a clear code than stalled.
* Messages are capped at `MAX_MESSAGE_BYTES` (1,000,000 bytes), and the
  API rate limits worker messages per session. The worker checks the
  size before it buffers an envelope and fails the turn with
  `worker_message_too_large`. Oversize or over-rate messages that still
  reach the API are rejected and counted.

## Worker robustness

These choices keep the worker side of the socket responsive and make
it recover cleanly. They were made for #487 and change nothing on the
wire.

**The receive loop only parses and dispatches.** Anything that can wait
for a reply from the API (a command, a guest teardown with its artifact
harvest, a revoke, a lease release) runs as a task. The reply that such
work waits for can only be delivered by the receive loop, and the
WebSocket library stops answering pings once its read queue is full, so
a loop that waits can deadlock itself and then lose the socket. Tasks
for the same session run in order: a command waits for a pending
teardown of its session. The cheap local part of a revoke (forgetting
the lease and the dedupe entries) still happens at once.

**Liveness.** The worker pings every 5 seconds and closes after 10
seconds without a pong, and it waits 15 seconds for `hello.reply`. A
half-open socket is then noticed within 15 seconds, half of the default
30 second lease TTL. A first frame that is not a usable `hello.reply`
leads to a reconnect, except for an explicit rejection (`unauthorized`,
`revoked`, `unsupported_protocol`, `token_bound`, `register required`,
`invalid register`, `shared_store_required`), which stops the process
with a clear message because retrying cannot help.

**Reconnect.** Exponential backoff with full jitter: a random delay up
to 0.5 seconds, doubling to a cap of 10 seconds, reset after a
connection that stayed up for 30 seconds. Full jitter was chosen over a
fixed step so that workers do not reconnect and replay together after
an API restart. The TLS context is built for every attempt, so a
rotated client certificate is used without a restart.

**The outbox sends each envelope once per connection.** A per-session
"sent" mark replaces the old rule of sending the whole unacked buffer
on every append, which made a replay quadratic and cost the API one
conflict per duplicate. A reconnect resets the mark, so everything
unacked is sent once more. The ingest stays idempotent, which makes the
resend safe.

**The disk spool is append-only.** An ack no longer rewrites the file.
An append reaches the operating system at once, so a killed process
loses nothing. The worker fsyncs the touched files once a second and at
shutdown, in a thread, which bounds the loss on a host crash to the
last second and keeps the disk off the event loop. Compaction of the
acked prefix runs in the same pass and thread. fsync on every append
was rejected because it costs a disk flush per envelope, and the lease
and the guest are lost with the host anyway.

**Drain and shutdown.** A drain ends when no Pi is live and the API
acked every buffered envelope, so the final `turn.status`, `usage`,
and `lifecycle.stop` are not lost. `--drain-timeout` bounds the whole
wait, also while the worker is disconnected. The killed-session harvest
needs the socket: it runs while the socket is open and is skipped with
a log line (`worker.harvest.skipped`) when it is not, instead of
waiting 60 seconds for a reply that cannot arrive. A presign waiter
fails at once when the socket closes after the API acked its request,
because the reply was most likely lost with the socket; a waiter whose
request is still unacked keeps waiting, because the replay delivers
both. Cancelling a turn passes through the upload code unchanged.

**Poison messages.** A malformed frame is logged, counted, and skipped.
A command that raises is answered with an `error` envelope and is not
recorded in the dedupe, so the retransmit runs it again; a stop that
raised is never acked as a duplicate. The worker tracks a lease only
for a command it accepted. A rejected `turn.start` releases its lease
after its failure is acked, and a `turn.cancel` for a session the
worker does not hold creates none. A lease the worker lets go while no
socket is open is released after the next `hello.reply`, so the claim
in `register` never drops a lease the API still holds. That was the one
worker side cause found for a spurious `worker_orphaned` after a
reconnect; the API side of the reconcile is unchanged here.

**Per-session outbox share.** One session may use half of the message
and byte bounds, so a noisy session or an outage fails its own turn and
not every turn on the worker. The emergency budget for the failure
itself still applies.

## Serving a socket on the API

The API replica that holds a worker socket serves it with separate
tasks, so no kind of work delays the messages that keep leases alive.
The decisions, and why:

* **The receive loop only parses and dispatches.** A control lane
  handles `heartbeat`, `lease.ack`, `lease.release`, `inventory`,
  `sandbox.seen`, and `store.proof`. An ingest lane applies durable
  envelopes in order. A delta lane publishes live deltas and drops them
  when it is full, because a delta is at-most-once anyway. The ingest
  lane has a bounded queue and makes the socket wait when it is full,
  because dropping a durable envelope would lose data. A
  `lease.release` for a session with envelopes still queued goes
  through the ingest lane so it cannot overtake them.
* **One writer task per connection.** Every send goes through its
  bounded queue of 1024 frames: replies and acks, commands from HTTP
  requests, and `lease.revoke` from the reaper. This makes the order
  of frames on one socket defined and leaves one place for a timeout.
  A frame that takes more than 10 seconds, or a full queue, closes the
  connection with the reason `write_timeout`. Forwarding commands
  between replicas builds on this path.
* **`hello.reply` is the first frame of the writer, and a connection is
  pickable only after it is queued.** Register restores leases and
  reconciles the inventory first, then queues `hello.reply`, then makes
  the connection visible to placement, commands, and the reaper.
* **Slow store work runs before the row lock and off the event loop.**
  Presign and `artifact.completed` read and hash the object (in chunks,
  in a thread) before the ingest transaction opens, so the session row
  lock and a database connection are never held across store calls. A
  store failure is kept and answered when the envelope applies, exactly
  as before.
* **The delta gate needs no database read.** Ingest records the turns
  whose final text or end it stored, in memory. The cost is that after a
  reconnect the record starts empty, so one straggling delta of a
  finished turn may be published. The final item replaces it.
* **An error on one message does not close the socket.** It is logged,
  counted, and retried where the sender does not retry (`lease.release`,
  and an ingest batch, three attempts each). Heartbeats, inventories,
  seen reports, and deltas are periodic or ephemeral, so the next one
  is the retry. Only these close a socket: a protocol violation, a
  revoked token, a superseded generation or a takeover, a write
  timeout, an ingest batch that still fails (so the worker reconnects
  and replays), and the peer going away. Binary and invalid JSON frames
  are skipped and counted, not treated as violations, because a single
  bad frame does not make the stream unsafe and closing would drop
  every other session of the worker.
* **`generation` is checked.** Every register bumps it. A heartbeat
  from a connection with an older generation renews nothing and the
  connection is closed with the reason `takeover`, so an old socket on
  another replica cannot keep leases alive for a worker that moved.
  Detach clears `api_instance_id` only when the detaching connection is
  still the current one on that replica.
* **The lease reaper commits first.** It clears rows and stores the
  error events in one transaction, and only then logs, updates memory,
  and sends `lease.revoke` through the writer with a 5 second timeout. A
  failed round is logged and counted and the loop runs again.
* **The frame limit is 4 MiB.** The API sets uvicorn's `ws_max_size` to
  4 MiB, a few times the 1 MiB envelope cap, which stays an
  application check that rejects and acks past.
* **A heartbeat with an invalid optional field keeps the lease.** The
  field is ignored and logged, and the lease is extended.

Every pool connection is shared by worker sockets, ingest, and HTTP.
`docs/production.md` gives the sizing rule: about two connections per
worker socket plus the concurrent requests.

## Auth

Workers authenticate with per-worker bearer tokens. Every secret
carries the fixed prefix `apipi_wk_` (plus `token_urlsafe(32)`), so the
public API recognises a worker bearer by its prefix and rejects it
without a database lookup. Only the SHA-256
hash is stored (`worker_tokens` table: id, name, hash, creation time,
last use, revocation). The secret is shown once at creation and never
again. The shared `APIPI_WORKER_TOKEN` is removed with no fallback.

* Setup, including test and development, uses
  `apipi workers token create|list|revoke` against the API database.
* The worker reads its token from `APIPI_WORKER_TOKEN_FILE` (a path to
  a file; surrounding whitespace is trimmed). `apipi worker` fails at
  startup when the setting is unset or the file is missing, unreadable,
  or empty.
* A token is valid only on `/internal/worker`. It is rejected with
  `401` on every other route.
* A token is bound to one `worker_id` on first register, or declared
  at creation. A register without `id` is assigned the bound
  `worker_id` by the API, which is how a restarted worker (which keeps
  no stable id of its own) keeps working. A register with a different
  explicit `id` is rejected with `token_bound`, whether or not the
  bound worker is connected. Several active tokens per worker allow
  rotation without downtime.
* Revocation is checked at register and on every heartbeat. A revoked
  token closes live sockets and new registers are rejected.

Transport security (TLS for non-loopback API URLs, optional mTLS)
lands with the final step of the epic. Tokens are bearer secrets until
then: keep the API URL on a trusted network or behind TLS.
