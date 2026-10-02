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
the API's `last_seq` on a new lease or reconnect.

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
`hello.reply{protocol: 2, sessions: {id: last_seq}}`.

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
after that seq.

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

Workers authenticate with per-worker bearer tokens. Only the SHA-256
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
  at creation. A register from another worker id is rejected while the
  bound worker is connected; after it disconnects the next register
  rebinds the token. Several active tokens per worker allow rotation
  without downtime.
* Revocation is checked at register and on every heartbeat. A revoked
  token closes live sockets and new registers are rejected.

Transport security (TLS for non-loopback API URLs, optional mTLS)
lands with the final step of the epic. Tokens are bearer secrets until
then: keep the API URL on a trusted network or behind TLS.
