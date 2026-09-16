# Multiple nodes

How you load-balance depends on whether Pi lives in the API process.

With `apipi serve --api-only` and `apipi worker`, a session is owned by
a **worker lease**. API replicas are interchangeable for create,
follow-up REST, and SSE. SSE also polls the store. You do not need
sticky routing for live Pi. `self_hosted` runner sockets still stick
to the API process that created them.

Combined `apipi serve` (no `--api-only`) still owns live Pi, local
`openai_hosted` directories, and in-memory SSE in that process.
Follow-up must return to that node, or the next turn has no Pi.

Scale by adding processes (systemd units or API containers), not
uvicorn workers inside one process. The Pi pool lives in one process.
Several processes share Postgres. Each combined process needs its own
SQLite file if you are not on Postgres.

Why workers exist is in [workers](worker-concepts.md).

## Topology

**Split (production).** N API processes, M workers, one Postgres, one
load balancer. APIs run `apipi serve --api-only`. Workers run `apipi
worker` with `APIPI_RUN_MODE=microvm` on KVM hosts. Point every API
and every worker at the same `DATABASE_URL`. Workers set
`APIPI_API_URL` and `APIPI_WORKER_TOKEN`. Give each worker its own
`APIPI_SESSIONS_DIR`. Artifact bytes can be local on the worker or S3.

The balancer can use least-conn (or round robin) for `/v1`. A turn
that cannot lease a worker returns `429` with code `capacity`. Worker
WebSockets are local to one API process. `workers.api_instance_id`
records that process (`APIPI_INSTANCE_ID`). Stick `/internal/worker`
to one API, or have each worker dial the API that will send it
commands. If a replica has the lease in Postgres but no socket, the
turn fails with `429` `capacity` and names the instance that holds the
socket. Session create, follow-up REST, and SSE still work on any
replica.

**Combined.** N gateway hosts, each `apipi serve` with
`APIPI_RUN_MODE=microvm`. Sticky hash on `session_id` so follow-up
hits the node that holds Pi. Give each process its own
`APIPI_SESSIONS_DIR`.

Set `APIPI_INSTANCE_ID` to a short name per API process (`node-a`).
When set, HTTP responses except `/health` include `X-ApiPi-Instance`.
Authenticated responses also include `X-Tenant-Id` and `X-User-Id`.
When a trace is known, responses include `X-Trace-Id`.

## Affinity

Official OpenAI clients put `session_id` in
`/v1/agents/sessions/{session_id}/…`. They do not store cookies by
default.

For **API-only plus workers**, hash is optional. Any replica can
stream SSE and accept the next message. Reconnect with `after_seq` to
replay from the store.

For **combined serve**, follow-up REST and SSE must return to the node
that owns Pi. Hash that path segment.

`/v1/environments/{environment_id}` is the `self_hosted` runner
WebSocket. That id is not the session id. For `self_hosted` with more
than one API process, use a dedicated tenant pool with one node, or
send the runner to the instance shown in `X-ApiPi-Instance` on create.

## nginx

Health checks should call `GET /health`. Probe health rather than a
session. Drain workers (`heartbeat` `"drain": true`) before you stop
a worker unit. Keep API health successful while a turn is in flight.

Long-lived SSE and WebSockets need buffering off and a long read
timeout. One hour matches a long turn plus idle.

API-only (no sticky Pi):

```
upstream apipi_api {
    least_conn;
    server 192.0.2.10:8000;
    server 192.0.2.11:8000;
}

server {
    listen 443 ssl;
    location /v1/environments/ {
        proxy_pass http://apipi_api;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
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

Combined serve still needs a session hash. Example:

```
map $uri $apipi_session {
    ~^/v1/agents/sessions/(?<sid>[0-9a-fA-F-]+) $sid;
    default "";
}

upstream apipi_session {
    hash $apipi_session consistent;
    server 192.0.2.10:8000;
    server 192.0.2.11:8000;
}
```

Send `/v1/agents/sessions/…` to `apipi_session` in that mode. The
`/v1/environments/` location is for a single-node tenant pool or for a
runner you already send to the owning instance.

## Tenant pools

Some tenants get their own gateway pool: a separate Host name or load
balancer backend group. Auth still returns `tenant_id`. You map that
tenant to the pool outside the gateway (DNS, balancer rule, or which
bearers the auth callback accepts on that pool). The public Agents API
does not change.

Shared: Postgres, and artifact bytes when `APIPI_ARTIFACT_STORE=s3`.
Isolated: live Pi and `APIPI_SESSIONS_DIR` on the worker (or on the
combined node).
