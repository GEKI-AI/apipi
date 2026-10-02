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
| `sessions` | `{session_id: last_seq}`: the persisted sequence per running session. The worker replays everything after that seq. Sequence persistence is not part of this step: the API always sends `last_seq: 0` as an explicit placeholder until the ingest and replay step lands. |

A first message that is not `register` is rejected with
`register required`. A bad register is rejected with
`invalid register`. Rejections are logged on the API and counted in
`apipi_worker_protocol_total{event}` (`unsupported_protocol`,
`invalid_register`, `unauthorized`, `revoked`, `token_bound`).

## Messages

JSON objects. `register` is the only pre-handshake message.

Worker to API:

| `type` | Fields | What |
| --- | --- | --- |
| `register` | See [Handshake](#handshake) | Create or reconnect the worker. |
| `heartbeat` | `capacity` (optional), `memory_mb` (optional), `run_mode` (optional), `accepts` (optional), `arch` (optional), `drain` (optional bool), `images` (optional list) | Refresh `last_seen`. May update caps, advertised `run_mode` and accepts set, architecture, drain posture, and the image list. |
| `lease.ack` | `id` (command id), `lease_id` | Command was received. Retransmits of the same id are safe. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. |
| `event` | `lease_id`, `event_type`, `data` | Persist a public session event. The worker must hold that lease. Unknown event types are ignored. |

The v2 envelope (`{v: 2, session_id, turn_id | null, seq, type,
payload}`) and its message schemas are defined in
`src/apipi/worker/protocol.py`, which the API and the worker share.
Durable ingest, the outbox, replay, and the cumulative ack land in
later steps; the schemas already describe that target.

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `protocol`, `worker_id`, `generation`, `sessions` | Register succeeded. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.cancel`, `turn.continue`, or `session.stop`. The `id` is the idempotency key. |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| error object | `ok: false`, `error` | Auth or register failed, then the socket closes. |

Message classes:

| Class | Types | Delivery |
| --- | --- | --- |
| Durable | `item.added`, `item.done`, `turn.status`, `usage`, `artifact.completed`, `error`, `sandbox.status` | Kept in the worker outbox until the cumulative `ack{last_seq}`. Ingested idempotently. |
| Ephemeral | `delta.text`, `delta.reasoning` | At-most-once, never persisted, never acked. The final item is the source of truth. |

The outbox is bounded (10,000 messages). When it is full the worker
pauses Pi output; if the turn cannot proceed it fails with
`worker_outbox_full`. Envelopes are capped at 1 MiB.

## Replay

Losing the socket does not abort a turn. A session is orphaned only
after the lease TTL, and the turn is not moved to another worker.

A reconnect may go to any replica. The worker sends its running
sessions with their `last_seq` in `register`; the API answers with
the persisted `last_seq` per session in `hello.reply` (always `0`
until sequence persistence lands); the worker
replays everything after that seq. Unacked commands are retransmitted
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
not kill idle guests; the worker that owns the session does. `none`
use `APIPI_IDLE_TTL`. Hosted computers use
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
