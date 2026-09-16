# Sandbox workers

Trusted ApiPi workers are the future host for Firecracker. They are
**not** customer `self_hosted` runners. `self_hosted` is an untrusted
computer that a tenant attaches with a per-session key on
`/v1/environments/{id}`. Workers are operator hosts. They use a
different path, a different secret, and different messages.

Firecracker, jailer, TAP, and the guest live on the **worker**.
`apipi serve --api-only` never probes `/dev/kvm` and never creates a
TAP device. Combined `apipi serve` (no `--api-only`) is the
single-host embedded worker: the same in-process adapter as today,
for a laptop or one box. Production is API-only plus one or more
`apipi worker` hosts.

`apipi worker` requires `APIPI_WORKER_TOKEN` and probes the configured
run mode before it connects. If `APIPI_RUN_MODE=microvm` cannot start,
the worker exits. It does not fall back to `none`.

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
| `register` | `id` (optional UUID), `capacity` (int ≥ 1) | Create or reconnect the worker. Reconnect bumps `generation` so a split brain cannot keep both sockets. |
| `heartbeat` | `capacity` (optional) | Refresh `last_seen`. |
| `lease.ack` | `id` (command id), `lease_id` | Command was received. Retransmits of the same id are safe. |
| `lease.release` | `session_id`, `lease_id` | Worker dropped the session. |
| `event` | `lease_id`, `event_type`, `data` | Persist a public session event. The worker must hold that lease. Unknown event types are ignored. |

API to worker:

| `type` | Fields | What |
| --- | --- | --- |
| `hello` | `ok`, `worker_id`, `generation` | Register succeeded. |
| `command` | `id`, `session_id`, `lease_id`, `op`, `payload` | `op` is `turn.start`, `turn.cancel`, or `turn.continue`. |
| `lease.revoke` | `session_id`, `lease_id` | Lease is no longer valid. |
| error object | `ok: false`, `error` | Auth or register failed, then the socket closes. |

## Leases

A lease is durable on the session row (`worker_id`, `lease_id`,
`lease_until`). Commands carry `lease_id`. A worker that does not hold
that lease cannot ack, emit events, or release it.

When `lease_until` passes, the API clears ownership, emits
`agent.session.error` with code `worker_lease_expired`, and sends
`lease.revoke` if the worker is still connected. It does not assign
the session to another worker in this version.

Reconnect with the same worker id replaces the old socket, increments
generation, and retransmits unacked commands for leases that worker
still owns.
