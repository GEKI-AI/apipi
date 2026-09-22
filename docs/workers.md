# Sandbox workers

This page is the operator reference: messages, leases, drain, and
which process needs KVM. Why workers exist and how a turn moves is in
[Workers](worker-concepts.md). Isolation of Pi is in
[isolation](isolation.md).

Trusted ApiPi workers host Firecracker. They are **not** customer
`self_hosted` runners. `self_hosted` is an untrusted computer that a
tenant attaches with a per-session key on `/v1/environments/{id}`.
Workers use a different path, a different secret, and different
messages.

Firecracker, jailer, TAP, and the guest live on the **worker**.
`apipi serve --api-only` never probes `/dev/kvm` and never creates a
TAP device. Combined `apipi serve` (no `--api-only`) is the
single-host embedded worker: the same in-process adapter as today,
for a laptop or one box. Production is API-only plus one or more
`apipi worker` hosts. Chat fleets add workers with
`APIPI_RUN_MODE=chat` next to `microvm`. See [chat fleets](chat.md).

`apipi worker` requires `APIPI_WORKER_TOKEN` and probes the configured
run mode before it connects. If `APIPI_RUN_MODE=microvm` cannot start,
the worker exits. It does not fall back to `none`.

`apipi serve --api-only` (or `APIPI_API_ONLY`) runs turns on a leased
worker. The API persists events from the store and streams SSE without
Pi on that node. If no worker can take a lease, the turn returns `429`
with code `capacity`. Combined `apipi serve` still runs turns
in-process.

Start everything through the ApiPi CLI:

```
apipi serve
apipi serve --api-only
APIPI_WORKER_TOKEN=secret APIPI_API_URL=http://api.example:8000 apipi worker
apipi check --role api
apipi check --role worker
apipi install --role api
apipi install --role worker
```

Combined `apipi serve` keeps today's single-host path. `--api-only`
skips the KVM probe so the API can run without Firecracker.
`apipi worker` is the sandbox process. It is not a `self_hosted`
runner.

## Auth

The worker opens an outbound WebSocket to `/internal/worker` and
sends `Authorization: Bearer <token>`. The token is
`APIPI_WORKER_TOKEN` on the API process. The gateway compares it in
memory. It does not store worker secrets in Postgres. If the token is
unset, the socket is rejected.

This is not mTLS yet. A later change can add it without changing the
message types.

## Messages

JSON objects. The first worker message must be `register`.

Worker to API:

| `type` | Fields | What |
| --- | --- | --- |
| `register` | `id` (optional UUID), `capacity` (int ≥ 1), `memory_mb` (int ≥ 1, optional), `run_mode` (string, required) | Create or reconnect the worker. `capacity` is max live sessions. `memory_mb` is the RAM budget in MiB. If `memory_mb` is omitted, the API uses `capacity ×` guest `mem_mib`. `run_mode` is the placement class this process serves (`chat`, `microvm`, or the process `APIPI_RUN_MODE`). Reconnect bumps `generation` so a split brain cannot keep both sockets. |
| `heartbeat` | `capacity` (optional), `memory_mb` (optional), `run_mode` (optional), `drain` (optional bool) | Refresh `last_seen`. May update caps, advertised `run_mode`, and drain posture. |
| `lease.ack` | `id` (command id), `lease_id` | Command was received. Retransmits of the same id are safe. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. |
| `event` | `lease_id`, `event_type`, `data` | Persist a public session event. The worker must hold that lease. Unknown event types are ignored. |

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `worker_id`, `generation` | Register succeeded. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.cancel`, `turn.continue`, or `session.stop`. |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| error object | `ok: false`, `error` | Auth or register failed, then the socket closes. |

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

Reconnect with the same worker id replaces the old socket, increments
generation, and retransmits unacked commands for leases that worker
still owns. The same `command.id` is replayed; the worker must treat
that id as idempotent so a turn is not run twice.

## Placement

`WorkerHub.pick` matches **placement class** before capacity or RAM.
A session is assigned only to a connected worker whose advertised
`run_mode` equals that class. There is no fallback to another mode.

| Session | Required worker `run_mode` |
| --- | --- |
| Session metadata `apipi.session_kind=chat` (`/v1/chat`) | `chat` always |
| Agents with a computer (`openai_hosted`, `hosted`, or `self_hosted`) | `microvm` |
| Agents with `environment.type=none` | `APIPI_ENV_NONE_PLACEMENT` / `[placement].env_none`: `chat` (default), `microvm`, or `reject` |

`reject` fails the turn with `400` and code `placement`. No matching
worker is `429` with code `capacity`, as today.

Commands include `run_mode` in the payload. The worker compares that
to its process `APIPI_RUN_MODE` and does not start Pi when they do not
match. `APIPI_RUN_MODE=chat` is the process name for a chat pool. It
uses the same host backend as `none`. Isolation `none` may still run a
`chat` command. A `none` or `chat` worker must not run a `microvm`
command, and a `microvm` worker must not run a `chat` command.

Set `APIPI_RUN_MODE=chat` on dedicated chat workers so they advertise
`chat`. Advertising `none` matches no Agents placement class.

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

Host Pi (`chat` / `none`) is a child of the worker. A graceful stop
runs pool teardown. A `kill -9` of the worker leaves those children.
The next worker start reaps leftovers stamped with a dead
`APIPI_WORKER_PID`. Set `KillMode=control-group` on the systemd unit
(`deploy/systemd/apipi-worker.service`) so `systemctl stop` kills the
cgroup. See [production](production.md#failure-and-drain).

## What runs where

| Process | Trust | Needs |
| --- | --- | --- |
| `apipi serve --api-only` | Operator control plane | Postgres, worker token, no KVM |
| `apipi worker` | Operator sandbox host | KVM, Firecracker, worker token, outbound to the API |
| Combined `apipi serve` | Lab / one box | Whatever the run mode needs, including KVM when `microvm` |
| `self_hosted` runner | Tenant computer | Per-session key on `/v1/environments/{id}` |

The worker token is an operator secret. It is not a tenant bearer and
is not stored in Postgres. Do not put it in the browser. The API
container in Compose is unprivileged. The worker unit is the only
place that should receive `/dev/kvm` and `CAP_NET_ADMIN`.
