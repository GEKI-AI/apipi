# 0015. Worker protocol v2

Only the API writes to Postgres. A worker is a just-in-time executor:
it gets session context and credentials with each command, keeps them
in memory only, and reports results over the worker WebSocket
(`/internal/worker`, protocol v2). It holds no database credentials
and no object-store credentials. A compromised worker host then sees
only the sessions it is currently running, not the whole database.

The public HTTP API is unchanged by this decision.

This record keeps the decision and its reasons. The wire contract is in
[the worker protocol specification](https://github.com/GEKI-AI/apipi/blob/main/docs/worker-protocol.md).

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

The worker dials one outbound WebSocket to `/internal/worker`. It dials
one URL, so it holds one socket, and a reconnect may land on any API
replica. The API pushes `command` messages; the worker sends envelopes
back. There is no queue system (Celery, Temporal). The follow-up
fan-out from the API to SSE clients uses Postgres `LISTEN/NOTIFY`
behind an `EventBus` interface, which leaves room for NATS or Redis
without touching the worker wire.

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
cursor in `hello`, and a new lease gets it in the command that starts
the work (see Sequence on a new lease). The first durable `seq` of a
session is 1.

The wire contract, with every message, field, and type, is in
[the worker protocol specification](https://github.com/GEKI-AI/apipi/blob/main/docs/worker-protocol.md).
This record keeps the decisions about the two message classes:

* **Durable** (14 envelope types): the worker keeps an outbox until it
  gets a cumulative `ack{session_id, last_seq}`. The API ingests
  idempotently: it claims a ledger row (`worker_ingest`, unique on
  `(session_id, worker_seq)`) inside a savepoint per envelope and treats
  a conflict as a duplicate. It ingests in batches and acks after
  commit. The final item is the source of truth, never the deltas.
* **Ephemeral** (`delta.text`, `delta.reasoning`): at-most-once, never
  persisted, never acked. Deltas are coalesced before fan-out. They
  carry their own per-session `seq` counter, which the API never checks.

API to worker: `command{id, session_id, op, lease_id, payload}`
(idempotent by `id`), cumulative `ack{session_id, last_seq}`,
`lease.revoke`, and `hello`.

## Handshake and version

The first worker message must be `register` with `protocol: 2`, the
sessions the worker still runs, and the session kinds it accepts. The
API rejects anything other than `protocol: 2` with close code 1008 and
reason `unsupported_protocol`, logs the rejection, and counts it in
`apipi_worker_protocol_total`. A hard cut is accepted: old workers
cannot connect, and there is no fallback.

`accepts` is a list of session kinds (`none`, `microvm`) and drives
placement: a session is assigned only to a worker whose accepts set
contains the kind that the session needs.

The API answers with `hello`, which carries the persisted `last_seq` per
running session. The cursors are persisted (`sessions.worker_seq`,
advanced by every ingest batch), so the worker replays exactly what the
API does not hold yet.

## Messages added after the first version

The first version had the envelope stream, commands, and the lease
messages. These messages were added later, each for one reason:

* **`search.request` and `search.reply`.** The search provider key lives
  only on the API, so the worker asks the API. It is a synchronous
  request keyed by `request_id`: not durable, not in the outbox, and
  never replayed, because a lost search is a tool error and not a turn
  failure.
* **`artifact.presign`, `artifact.presign.reply`, and
  `artifact.completed`.** The worker holds no object-store credentials.
  The API issues a presigned PUT URL (S3) or a store-root relative path
  (filesystem store), and the worker reports the finished upload. The
  reply is stored with the upload slot, so a replay of the envelope gets
  the same reply.
* **`inventory` and `inventory.reply`.** The worker reports its live set
  so the API can reconcile leases, and the reply carries revokes and
  reaper TTLs. This gives the worker a reaper without a database.
* **`sandbox.seen`.** The worker reports the live sandbox ids, and the
  API applies the `touch_seen` update. It replaces the worker writing
  `touch_seen` itself, which needed a database connection.
* **`store.proof`.** The filesystem store needs the API and the worker
  to share one root. The API writes a marker file and a nonce, and the
  worker proves it can read them.

## Leases, sequence, and release

Three rules keep a running turn and its results from vanishing without
an error. Each was chosen as the smallest change that holds.

**Heartbeats come from a timer.** The worker sends `heartbeat` from its
own task on a monotonic timer, not when the socket has been quiet. The
API acks every ingest batch, so a busy turn keeps the socket from ever
being quiet, and a quiet-socket heartbeat starved until the lease
expired. The inventory and the drain check run on timers too. The API
owns the numbers: `hello` carries `lease_ttl_seconds` and
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
reports `session.stopped` (see Delivery guarantees), only after the API
acked every envelope buffered for that session, with a timeout (10 seconds) after
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

**A release does not end a lease that a new command already uses.**
The worker keeps the lease while a turn command runs and releases it
only after the command ended without a live process. A release can
still cross a `turn.start`, `turn.continue`, or `sandbox.boot` that
the API already sent on that lease; the API ignores such a release,
because the worker takes the lease back from the command. This was
chosen over a new release field or a release acknowledgement, because
the command already carries its `lease_id` and the API can decide from
the order of the socket alone.

**A request waits for a release that is in flight.** The API takes
the lease out of the connection before it clears the session row, so
that a command sent meanwhile cannot use it. A command or a placement
for that session waits on the replica that handles the release until
the release ended, then places the session on a new lease. The wait is
bounded (5 seconds from the start of the release, below the 10 second
forward timeout, so a forwarded command still gets its answer), and a
release that does not finish leaves the request with the error it had
before. A requesting replica whose forwarded command found the lease
gone answers as for a session without a lease (a message places the
session itself), and one that finds another lease on the row sends the
command once more on it. This was chosen over sending the
command on the half-released lease, which the worker would take back
only to have the API clear it, and over a new protocol message.

## Semantics

* Losing the socket does not abort a turn. A session is orphaned only
  after the lease TTL, and the turn is not moved to another worker:
  the guest and workspace were on the expired host.
* A reconnect may go to any replica. It replays after `hello`.
* The outbox is bounded (`OUTBOX_BOUND`, 10,000 messages, and a byte
  bound). One session may use at most half of each bound. When a bound
  is hit the turn fails with `worker_outbox_full`. The worker does not
  pause Pi: pausing the output stream would need a backpressure path
  from the outbox into the Pi RPC reader, and a turn that cannot report
  its results is better failed with a clear code than stalled.
* Messages are capped at `MAX_MESSAGE_BYTES` (1,048,576 bytes), and the
  API rate limits only `delta.*` envelopes (100 per second per session,
  with a text cap of 32 KiB). Every other message is only size capped.
  The worker checks the size before it buffers an envelope and fails the
  turn with `worker_message_too_large`. Oversize messages and
  over-rate deltas that still reach the API are rejected or dropped and
  counted.

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
seconds without a pong, and it waits 15 seconds for `hello`. A
half-open socket is then noticed within 15 seconds, half of the default
30 second lease TTL. A first frame that is not a usable `hello`
leads to a reconnect, except for an explicit rejection (`unauthorized`,
`revoked`, `unsupported_protocol`, `token_bound`, `register_required`,
`invalid_register`, `shared_store_required`), which stops the process
with a clear message because retrying cannot help. A rejection with
`register_timeout` is retryable: the API sends it when no first message
arrives within 15 seconds.

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
socket is open is released after the next `hello`, so the claim
in `register` never drops a lease the API still holds. That was the one
worker side cause found for a spurious `worker_orphaned` after a
reconnect; the API side of the reconcile is unchanged here.

**Per-session outbox share.** One session may use half of the message
and byte bounds, so a noisy session or an outage fails its own turn and
not every turn on the worker. The emergency budget for the failure
itself still applies.

## Delivery guarantees and forward compatibility

The goal is that a durable envelope is either applied or rejected for a
permanent reason, a command reaches the worker or fails visibly, a
request with a reply always gets its reply, and a newer peer never
loses data on an older peer during a rolling upgrade. The per-message
table is in `docs/workers.md` (Delivery guarantees). These are the rules
and why each was chosen.

**Permanent and temporary failures.** An error while applying an
envelope is classified. A deadlock, a lock or statement timeout, a
connection reset, and a temporary object-store error (timeouts,
throttling, 5xx, also on `artifact.completed`) are temporary. The API
does not ack the envelope or anything after it in that session, tries
the batch again, and after three tries closes the socket so that the
worker replays. Everything else is a verdict on the envelope and is
rejected, logged, counted, and acked past, as before. Acking past a
temporary error was rejected because it turns a database hiccup into
lost billing data and lost artifacts. Stalling only the affected
session keeps the other sessions of the socket moving.

**Unknown fields are ignored and counted.** The `extra` policy of every
model is "ignore", set in `protocol/base.py`. An ignored field is
counted (`unknown_field`) and logged, so a version skew is visible. A
test switch (`strict_parse`) makes the models that used to forbid
extras (envelope payloads and the command context) reject them again,
and the whole test suite runs with it on, so a typo in a sender of this
repository still fails there. Ignoring was chosen over forbidding
because a rolling upgrade always has a newer sender: forbidding lost
envelopes (the next cumulative ack skips the dropped one) and failed
turns.

**Features.** `register` and `hello` carry an optional `features` list.
A peer without it is a baseline peer, so an old peer keeps working. The
rules:

* An additive field is always allowed and needs no feature.
* A new message type or command `op` needs a feature. A peer sends it
  only when the other side listed the feature. A new envelope type that
  an old API does not know cannot be acked safely, because a later
  cumulative ack would skip it.
* Removing a field or changing its meaning needs a new protocol version.
  The API rejects another `protocol` with `unsupported_protocol`.
* A change of behavior that an old peer cannot parse is a feature too.
  `session_stopped` is one: it moves the ack of `session.stop` to the
  receipt and makes the durable `session.stopped` envelope the
  completion. A baseline peer keeps the old behavior on both sides.

The features today are `search`, `presign`, and `lease_cursor`, which
describe the protocol that existed before features and are in the
baseline, and `session_stopped`, `image_refs`, `file_refs`, and
`session_files`, which are not. `image_refs` sends input images in
`turn.start` as store references instead of base64 bytes, so an image no
longer counts toward the command size limit. Workers before it uploaded
input images with the `artifact.presign` kind `input_image`, which the
API presigned to the final key of the file. That kind is removed: the
API answers it with `ok: false` and `artifact_store` and presigns no
URL, so the API never presigns a PUT to a `files` or `skills` key for a
worker. `file_refs` does the same
for the `input_file` parts of a session without a computer: the worker
reads a text file from the store and puts its text in the prompt.
`session_files` adds the attachments of a session with a computer: the
context lists them as references in `session_files`, the worker writes
the missing ones into the workspace, and a `turn.start` part names the
path of each new one. The `hello` of the API
must carry `lease_ttl_seconds` and `heartbeat_seconds`: a worker cannot
guess a safe heartbeat, so a `hello` without them stays an error. A
receiver does not ack an unknown `op` as done, so the API sends it again
and finally fails it.

**Commands are a queue.** The API keeps unacked commands per lease in
send order (a small queue) instead of one slot per lease, sends them
again on every reconnect for every lease of the worker, including a
lease the worker did not claim, and on a 5 second timer while connected,
and fails a command that stays unacked for the lease TTL by clearing the
lease and failing the turn. The queue is held by the replica that holds
the socket. The case that made a lease live forever was a command written
to a socket that died before the worker read it. The worker reconnected
with a claim that lacked the lease, the API skipped the lease, and
heartbeats kept it alive.

**Presign replies.** The upload slot stores the worker `request_id`. A
replayed `artifact.presign` is answered from that slot, so the reply is
the same. The API sends the reply before the ack of the envelope, so a
worker that got the ack always got the reply before it.

**Stop.** All commands are acked on receipt. The completion of
`session.stop` is the durable `session.stopped` envelope, and the API
drops the lease when ingest applied it (or after 15 seconds).

**Sizes are bytes.** Limits are bytes of the UTF-8 JSON text frame as
sent: `MAX_MESSAGE_BYTES` is 1,048,576 and `MAX_COMMAND_BYTES` is
262,144 (binary units, so that the docs and the constants agree). The
worker checks before it buffers, and the API checks a command before it
sends it.

**A worker restart.** A restarted worker has no lease claims. A register
with an empty claim no longer orphans the leases of that worker at once,
so the spooled envelopes are applied. The first inventory orphans what
is really gone. What a restart can still lose is listed in
`docs/workers.md`: envelopes of a lease that expired or was released
before the worker came back are rejected as `not_leased`.

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
* **`hello` is the first frame of the writer, and a connection is
  pickable only after it is queued.** Register restores leases and
  reconciles the inventory first, then queues `hello`, then makes
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

## Routing commands across API replicas

A worker holds one socket, to one replica. Every other replica must
still be able to start a turn on it, cancel, stop, boot, and revoke a
lease, and must see its capacity when it places a session. The worker
wire does not change: forwarding is internal to the API replicas. The
decisions, and why:

* **Forward over the `EventBus`, with a small database mailbox.** A
  command body can hold the turn text and image file ids, up to
  262,144 bytes, and `NOTIFY` carries at most 8000. The requesting replica
  therefore inserts one row into `worker_forwards` (the request, never
  the context) and sends a tiny `forward` message with only the row id
  to the replica that holds the socket. The row is the durable part, so
  a lost notification is recovered by a fallback poll
  (`APIPI_EVENT_BUS_FALLBACK_POLL`) on the receiving side.
* **One channel per replica.** Every API process has an instance id
  (`APIPI_INSTANCE_ID` or the host name, plus a random suffix, so a
  restart is a new instance). `workers.api_instance_id` holds it. A
  replica listens on its own channel only. Two message kinds exist:
  `forward` goes to the replica that holds the socket and
  `forward_result` goes back to the replica that asked. Both carry the
  id and a status, never a body. `InMemoryEventBus` delivers them in
  process, which is only ever one replica; `PostgresEventBus` uses
  `LISTEN` and `pg_notify` through the same publish lock as every other
  message.
* **The owning replica builds the context.** It reads the session, the
  agent, the vault, and the object store itself, so no vault header,
  presigned URL, or file reference is ever stored in the mailbox or
  sent over `NOTIFY`. An input image keeps only its `file_id` in the
  row, and the owning replica checks it for the tenant and signs the
  image reference itself. The same holds for the model key: the request
  bearer is never written to Postgres (constitution rule 5), so it is
  not forwarded. The row keeps `key_id`, `user_id`, `org_id` and the
  tenant, and the owning replica resolves the key from that identity
  with the `model_credential` callback (or `OPENAI_API_KEY_OVERWRITE`).
  With neither, the forward fails with `503` `model_key_unavailable`.
  The worker applies `context.model.api_key` to the credential broker at
  the start of every turn, so Pi does not restart when the key changes.
* **The result is the same as a local send.** A forward asks for one
  wait level: `sent` (the frame is in the writer of the worker's
  connection), `ack` (the worker sent `lease.ack`), `stopped` (a
  `session.stop` ack and then the durable `session.stopped`, or the
  15 second stop timeout, exactly like a local stop), or `none` (the
  reaper's revoke, which does not wait). The owner advances the row
  through `claimed`, `sent`, `acked`, and `done`, or `failed` with an
  error `code`, a message, and an HTTP status. The requesting replica
  wakes on `forward_result` and rereads the row every second as
  well. An owner error (`unsupported_op`, `capacity`,
  `image_unavailable`, `payload_too_large`) reaches the client as the
  same `ApiError` a local send would raise. The ack and stop waits,
  and `note_stopped`, therefore stay in the process that holds the
  socket and ingests `session.stopped`; only the outcome travels.
* **Forwards are idempotent.** The row id is the command id. The
  owner claims a row with a conditional `UPDATE` from `pending` to
  `claimed`, so a duplicate notification or poll does nothing. A
  command is enqueued once under its id, and a repeated send is
  absorbed by the worker, which already de-duplicates by command id.
  Commands then follow the same queue, retransmit timer, and lease TTL
  expiry as local ones, on the owner.
* **Staleness.** A worker row is live on another replica when
  `api_instance_id` is set and `last_seen` is newer than the lease
  TTL. Heartbeats arrive every third of the TTL, so that is about two
  missed heartbeats. A stale row means the replica is gone: the command
  fails at once with `503` `worker_unreachable`, and placement skips the
  worker. The worker reconnects to a live replica, and the lease is
  taken over as before. An owner that stays silent for 10 seconds
  after the row was written (a crash inside the staleness window)
  gives `504` `forward_timeout`. A clean detach clears the instance,
  and a command then behaves as it does today for a worker that is
  not connected.
* **Placement sees the fleet.** The `workers` rows now also hold
  `accepts`, the image ids, `arch`, and `draining`, written on
  register and on every heartbeat next to `capacity`, `memory_mb`, and
  `last_seen`. Lease counts and memory come from the leased session
  rows, which are exact. Placement joins the local connections, which
  are authoritative, with the live rows of other replicas, and uses the
  same ordering (most free RAM, then fewer leases). Capacity, images,
  and drain state of a remote worker are at most one heartbeat old
  (at most 10 seconds). The owner checks capacity again when it grants
  the lease and answers `capacity` when the worker filled up in
  between, so a stale read can cost a retry but never an overcommit.
  `acquire` on a remote worker is forwarded whole, so the lease grant,
  the first send, and the delta bookkeeping all happen on the owner.
* **Release, revoke, and the reaper.** Whoever clears a lease row also
  tells the owner to drop the lease from its connection and queue
  (`revoke` without a frame), and the reaper asks it to send
  `lease.revoke` as well. Delete (`session.stop`, then release) runs
  from any replica.
* **A cancel that cannot reach the worker is an error.** `cancel`
  returns `503` `worker_unreachable` (or `504` `forward_timeout`) when
  a lease exists and the command was not delivered. A session with no
  lease has nothing to cancel and answers as before. Delete is the
  exception: when `session.stop` cannot be delivered, it logs
  `worker.stop.undelivered`, clears the lease, and deletes the session,
  and the guest is dropped at the worker's next inventory.
* **Single replica.** `InMemoryEventBus`, which SQLite uses, switches
  forwarding and fleet placement off. Several processes need
  Postgres, which gives `PostgresEventBus`.

The cost of a forward is one insert, one claim, one to three status
updates, and one delete on a small table, plus two notifications. The
rejected alternatives are a worker that dials every replica (it
multiplies sockets, leases, and inventories and needs replica
discovery), a sticky load balancer for `/internal/worker` (it does not
solve placement, and a replica restart moves the workers anyway),
carrying the command in `NOTIFY` (the size limit), and splitting a
body over several notifications (it is not durable and not ordered).

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

Transport security has landed: the worker requires TLS for a
non-loopback API URL and supports optional mTLS. Tokens are bearer
secrets, so the socket is encrypted outside local development. See
`docs/workers.md` (Transport security).
