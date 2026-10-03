# 0015. Worker protocol v2

Only the API writes to Postgres. A worker is a just-in-time executor:
it gets session context and credentials with each command, keeps them
in memory only, and reports results over the worker WebSocket
(`/internal/worker`, protocol v2). It holds no database credentials
and no object-store credentials. A compromised worker host then sees
only the sessions it is currently running, not the whole database.

The public HTTP API is unchanged by this decision.

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
* The outbox is bounded (`OUTBOX_BOUND`, 10,000 messages). When it is
  full the worker pauses Pi output; if the turn cannot proceed it fails
  with `worker_outbox_full`.
* Messages are capped at `MAX_MESSAGE_BYTES` (1 MiB), and the API rate
  limits worker messages per session. Oversize or over-rate messages
  are rejected and counted.

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
