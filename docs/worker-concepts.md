# Workers

A worker is an operator process that runs Pi and the sandbox. It is
not a tenant `self_hosted` runner. Runners attach with a per-session
key on `/v1/environments/{id}`. Workers use a different URL, a
different secret, and different messages. Protocol fields are in
[sandbox workers](workers.md).

This page explains why the split exists, how a turn moves through it,
and how that changes scale.

## Why split

The HTTP API should be easy to run: rootless, several replicas, no
KVM. Firecracker needs `/dev/kvm`, TAP, and usually extra
capabilities. Putting both in one process forces every API node to be
a hypervisor host and forces sticky load balancing because Pi lives in
that process.

The split is:

| Process | Job |
| --- | --- |
| `apipi serve --api-only` | Auth, sessions, event log, SSE, scheduling. No TAP. |
| `apipi worker` | Firecracker (or `none`), Pi, workspace, harvest. Outbound to the API. |
| Combined `apipi serve` | Both in one process. Laptop or a single box. |

Combined serve is the embedded worker: the same in-process adapter the
project started with. Production is API-only plus one or more workers.

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
  apipi worker
       |  Firecracker guest (or none)
       |  Pi talks to OPENAI_BASE_URL
       |  persist public events in Postgres
       v
  the same API replica
       |  SSE reads the store (and a local hub)
       v
  client
```

The API does not spawn Pi. It sends `turn.start` (or `turn.continue` /
`turn.cancel`) on the worker socket. The worker runs the turn through
the same execution contract combined serve uses in-process. Public
events land in the store before the client sees them on SSE. If the
SSE connection sits on another API replica, that replica polls the
store. Pi does not have to live on the API node.

If no worker can take a lease, the turn returns `429` with code
`capacity`.

A heartbeat may set `"drain": true`. That worker keeps current leases
and takes no new ones. When `lease_until` passes, the lease is
cleared and the session gets `agent.session.error` with code
`worker_lease_expired`. The turn is not moved to another worker: the
guest was on the expired host. Start a new message after that.

## Combined vs split

On a laptop you want one command:

```
export OPENAI_BASE_URL=http://your-model-host/v1
apipi migrate
apipi serve
```

Isolation defaults to `none`. For guests on that same box, set
`APIPI_RUN_MODE=microvm` and use combined serve. The process probes
KVM, then serves HTTP.

In production the API can be a container and the hypervisor stays on
the host:

```
# API host or Compose
apipi serve --api-only

# KVM host
APIPI_API_URL=http://api.example:8000 \
APIPI_WORKER_TOKEN=secret \
APIPI_RUN_MODE=microvm \
  apipi worker
```

Copy-paste recipes are on [install](install.md). Unit files are in
`deploy/systemd/`.

## Trust

| Secret | Who | Where |
| --- | --- | --- |
| Tenant bearer | Your app | `Authorization` on Agents API routes. Mapped to a tenant. Not stored. |
| `APIPI_WORKER_TOKEN` | Operator | Worker `Authorization` on `/internal/worker`. Compared in memory. Not in Postgres. |
| Environment `key` | Tenant runner | `hello` on `/v1/environments/{id}`. One session. |

Do not put the worker token in a browser. Do not reuse it as a tenant
key. `self_hosted` is the customer's computer; they sandbox it. The
worker is yours.

## What this is not

Flintlock, Kata, and firecracker-containerd are not the worker. ApiPi
starts Firecracker on the worker. Those managers remain possible later
if they prove a clear win. See
[ADR 0010](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0010-apipi-firecracker.md)
and [isolation](isolation.md).
