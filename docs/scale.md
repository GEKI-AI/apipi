# Multiple nodes

`apipi serve` is always the API, and Pi runs in `apipi worker`
processes. A session is owned by a **worker lease**. API replicas are interchangeable for create,
follow-up REST, and SSE. Stored events fan out over the event bus,
so an SSE client on any replica sees commits from any other replica.
You do not need sticky routing for live Pi.

Scale by adding processes (systemd units or API containers), not
uvicorn workers inside one process. Several API processes share
Postgres. A SQLite file belongs to one API process.

Why workers exist is in [workers](worker-concepts.md).

## Topology

**Production.** N API processes, M workers, one Postgres, one
load balancer. APIs run `apipi serve`. Workers run `apipi
worker`; hosts that serve computer sessions use
`APIPI_RUN_MODE=microvm` on KVM hosts, and hosts that serve only
text-only sessions use `APIPI_RUN_MODE=none` with
`APIPI_WORKER_ACCEPTS=none` and no KVM. The three fleet layouts are
one worker type that does both (`none,microvm`), separate `microvm`
and `none` workers, or `none`-only. See
[sandbox workers](workers.md#placement) for `APIPI_WORKER_ACCEPTS`,
its defaults, and the per-worker supervision (`APIPI_PI_MEM_MIB`,
`KillMode=control-group`, the orphan reaper). Point every API
process at the same `DATABASE_URL`; workers never see it (`apipi
worker` refuses to start when `DATABASE_URL` is set, since only the
API writes to Postgres). Workers set
`APIPI_API_URL` and `APIPI_WORKER_TOKEN_FILE` (one token per worker,
created with `apipi workers token create`). Give each worker its own
`APIPI_SESSIONS_DIR` for its workspaces. Artifact, file, skill, and Pi
session bytes go through the configured store, never over the worker
socket. The recommended production setup is `APIPI_ARTIFACT_STORE=s3`:
the API issues presigned PUT and GET URLs, the worker uploads and
downloads directly, and store credentials exist only on the API. The
filesystem store (`APIPI_ARTIFACT_STORE=local` with `APIPI_LOCAL_STORE_DIR`, which
defaults to `.apipi/store`) is supported only when the API and every
worker mount the same store root at the same path (same machine or a
shared network filesystem). How to share it is up to the operator.
At register the API writes a nonce marker file into the root and
sends it in `hello.reply`; a worker that cannot read it back is
rejected with `filesystem store requires a shared path`.

The balancer can use least-conn (or round robin) for `/v1`. Workers
advertise a session cap and a RAM budget. Placement prefers free RAM
and will not oversubscribe either. A turn that cannot lease a worker
returns `429` with code `capacity`. Worker
WebSockets are local to one API process. `workers.api_instance_id`
records that process (`APIPI_INSTANCE_ID`). Stick `/internal/worker`
to one API, or have each worker dial the API that will send it
commands. If a replica has the lease in Postgres but no socket, the
turn fails with `429` `capacity` and names the instance that holds the
socket. Session create, follow-up REST, and SSE still work on any
replica.

For a single host in development, `apipi dev` starts one API and one
worker as two child processes. It is not a scaling topology.

Set `APIPI_INSTANCE_ID` to a short name per API process (`node-a`).
When set, HTTP responses except `/health` include `X-ApiPi-Instance`.
Authenticated responses also include `X-Tenant-Id` and `X-User-Id`.
When a trace is known, responses include `X-Trace-Id`.

## Affinity

Official OpenAI clients put `session_id` in
`/v1/agents/sessions/{session_id}/…`. They do not store cookies by
default.

Session hashing is optional. Any replica can stream SSE and accept the
next message. Reconnect with `after_seq` to replay from the store.

There is no runner WebSocket. `self_hosted` is currently not supported (see [environments](environments.md)).

## Event fan-out

Session events fan out over the event bus. After the API commits
stored events, it publishes a wake with the session id and sequence
number. SSE streams wait for wakes instead of polling the store, and
then read everything after their last sequence number. A wake that is
lost (for example while the listener reconnects) is covered by a
fallback poll (`APIPI_EVENT_BUS_FALLBACK_POLL`, default `3s`), so an
idle stream queries the store no more often than that interval.

On Postgres the bus uses `LISTEN`/`NOTIFY` on the `apipi_events`
channel, with one dedicated `LISTEN` connection per API replica. On
SQLite the bus stays in memory, which is why SQLite replicas cannot
share SSE. Live `output_text.delta` batches are coalesced over about
40ms and travel inside `NOTIFY` payloads under the 8000-byte limit;
they are never stored. See [config](config.md) for the settings and
[observability](observability.md) for the bus metrics.

The worker does not publish deltas itself. It sends
them as ephemeral `delta.text` envelopes over its worker socket (one
per coalesced batch, at-most-once, never acked). The API replica that
holds the socket checks that the session is leased to that worker,
applies size and rate limits, drops deltas for turns that already
committed their final text, and publishes the rest as `live`
messages on the bus. Reasoning deltas (`delta.reasoning`) are
accepted but never fanned out. SSE clients can sit on any replica;
only the worker socket itself stays pinned to one API process.

Only the API writes turns, items, events, and usage: the worker
buffers results in its outbox until the cumulative ack and replays
after `hello.reply`, then the ingesting replica publishes the
`EventBus` wake after commit. Turn context arrives in commands, so the
worker needs no database access.

## nginx

Health checks should call `GET /health`. Probe health rather than a
session. Drain workers (`heartbeat` `"drain": true`) before you stop
a worker unit. Keep API health successful while a turn is in flight.

Long-lived SSE and WebSockets need buffering off and a long read
timeout. One hour matches a long turn plus idle.

The API needs no sticky routing:

```
upstream apipi_api {
    least_conn;
    server 192.0.2.10:8000;
    server 192.0.2.11:8000;
}

server {
    listen 443 ssl;
    location / {
        proxy_pass http://apipi_api;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

## Tenant pools

Some tenants get their own gateway pool: a separate Host name or load
balancer backend group. Auth still returns `tenant_id`. You map that
tenant to the pool outside the gateway (DNS, balancer rule, or which
bearers the auth callback accepts on that pool). The public Agents API
does not change.

Shared: Postgres, and artifact, file, skill, and Pi session bytes through the configured store (`s3` or one shared `APIPI_LOCAL_STORE_DIR`).
Isolated: live Pi and per-worker `APIPI_SESSIONS_DIR` workspaces on the worker.
