# Workers

A worker is an operator process that runs Pi and the sandbox. It is
not a tenant computer. Workers attach with a lease on `/internal/worker` over worker protocol v2. Workers use a different URL, a
per-worker token, and versioned messages. The socket needs TLS for
any non-loopback API URL, with optional mutual TLS, and the worker
holds no database or object-store credentials: it gets everything it
needs in the command context and reports back over the socket.
Message shapes are in
[the worker protocol specification](worker-protocol.md); operator
settings are in [sandbox workers](workers.md). The decision and its
reasons are in
[ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md).

This page explains why the API and the workers are separate processes,
how a turn moves between them, and how that changes scale.

## Why two processes

The HTTP API should be easy to run: rootless, several replicas, no
KVM. Firecracker needs `/dev/kvm`, TAP, and usually extra
capabilities. If both lived in one process, every API node would have
to be a hypervisor host, and load balancing would have to be sticky
because Pi would live in that process.

The two processes are:

| Process | Job |
| --- | --- |
| `apipi serve` | Auth, sessions, event log, SSE, scheduling. Always the API: no Pi, no TAP, no idle reap. |
| `apipi worker` | Firecracker (or `none`), Pi, workspace, harvest, idle reap. Outbound to the API. |

Production is one or more API processes plus one or more workers, and
`apipi dev` starts one of each for local development. Sessions without a computer (`environment.type=none`) run on workers whose
accepts set contains `none`, on that same API. See [sandbox workers](workers.md#placement).

## How a turn moves

Example: the client posts a follow-up. Two API replicas sit behind a
load balancer. A worker already registered.

```
  OpenAI SDK
       |
       |  POST /v1/agents/sessions/{id}/events
       v
  any API replica          bearer → tenant
       |                   append nothing yet
       |  lease a worker (durable on the session row)
       v
  apipi worker             wss + per-worker token, no DB credentials
       |  Firecracker guest (or none)
       |  Pi talks to OPENAI_BASE_URL
       |  durable results buffered in the outbox, sent as v2 envelopes
       v
  the same API replica (or any replica after a reconnect)
       |  ingests envelopes idempotently, acks, then wakes SSE
       |  SSE reads the store (and a local hub)
       v
  client
```

The API does not spawn Pi. It sends `turn.start` (or `turn.continue` /
`turn.cancel`) on the worker socket. When that socket is on another API
replica, the replica that took the request hands the command to the one
that holds the socket, which sends it ([forwarding](workers.md#commands-across-api-replicas)). Each command carries a `context`
object that the API builds just in time: the session environment and
identity, the agent definition, the effective idle TTL, the HTTP MCP
servers with vault headers applied, the model key, and references (never
bytes) to workspace files, skills, and the Pi session blob. The worker
holds the context in memory only and prepares the turn from it, without
reading the database. The worker then runs the turn and reports back in v2 envelopes: durable results (`item.added`,
`turn.status`, `usage`, `event`, `session.status`, `session.stopped`,
`workspace.reaped`, `lifecycle.start`, `lifecycle.stop`, `error`,
`sandbox.status`, `artifact.presign`, and `artifact.completed`) that
the API ingests idempotently, sandbox seen summaries and inventory live
sets on the same socket, and ephemeral
streaming deltas (`delta.text`, `delta.reasoning`) that are never
persisted. Only the API writes to Postgres: the worker buffers
results in a bounded outbox (with an optional disk spool) until the
cumulative ack, and replays after `hello` on reconnect, so a
dropped socket or an API restart mid-turn loses nothing and duplicates
nothing. The worker holds no object-store credentials, so it asks the
API for an upload slot with `artifact.presign`. The API answers with
`artifact.presign.reply` on the same socket: a presigned PUT URL for
S3, or a store-root relative path for the filesystem store. The worker
uploads the bytes and then reports `artifact.completed`. Public events land in the store before the client sees them
on SSE. If the SSE connection sits on another API replica, that
replica wakes over the event bus. Pi does not have to live on the API
node.

The built-in `web_search` tool is the one call that does not use the
outbox. The worker sends a synchronous `search.request` and waits for a
`search.reply`, because the search provider key lives only on the API.
A lost socket is a tool error, not a turn failure, and the worker does
not replay the request. See [workers](workers.md#search-requests).

If no worker can take a lease (session cap or RAM budget), the turn
returns `429` with code `capacity`. If workers are live for the run
mode but none has the session's guest image, the turn returns `503`
with code `image_unavailable`. Images are files the operator pulls
onto the worker before it starts. They are not built on every host.

A heartbeat may set `"drain": true`. That worker keeps current leases
and takes no new ones. A worker that reconnects, to any replica,
renews its reattached leases and replays its outbox, so running turns
survive the move. About every 60s it also reports its live set as
`inventory`; the API fails leases the worker no longer reports and
revokes sessions the worker reports without a lease. When
`lease_until` passes, the lease is
cleared and the session gets `agent.session.error` with code
`worker_lease_expired`. The turn is not moved to another worker: the
guest was on the expired host. Start a new message after that.

## Development and production

On a laptop you want one command:

```
export OPENAI_BASE_URL=http://your-model-host/v1
apipi dev
```

`apipi dev` runs `apipi migrate`, then starts `apipi serve` and
`apipi worker` as two child processes. Isolation defaults to `none`.
For guests on that same box, set `APIPI_RUN_MODE=microvm`. The worker
probes KVM before it connects.

In production the API can be a container and the hypervisor stays on
the host:

```
# API host or Compose
apipi serve

# KVM host
apipi workers token create --name worker-1   # prints the secret once
APIPI_WORKER_TOKEN_FILE=/run/apipi/worker.token \
APIPI_API_URL=https://api.example \
APIPI_RUN_MODE=microvm \
  apipi worker
```

Copy-paste recipes are on [install](install.md). Unit files are in
`deploy/systemd/`.

## Trust

| Secret | Who | Where |
| --- | --- | --- |
| Tenant bearer | Your app | `Authorization` on Agents API routes. Mapped to a tenant. Not stored. |
| Per-worker token | Operator | Worker `Authorization` on `/internal/worker`. Only the hash is stored in Postgres. Rejected everywhere else. |


Do not put the worker token in a browser. Do not reuse it as a tenant
key. The computer always shares Pi's isolation boundary. The
worker is yours.

## What this is not

Flintlock, Kata, and firecracker-containerd are not the worker. ApiPi
starts Firecracker on the worker. Those managers remain possible later
if they prove a clear win. See
[ADR 0010](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0010-apipi-firecracker.md)
and [isolation](isolation.md).
