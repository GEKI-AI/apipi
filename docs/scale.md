# Multiple nodes

One combined `apipi serve` process owns its live Pi processes, local
`openai_hosted` directories, artifact bytes, SSE subscribers, and
`self_hosted` runner sockets. Those stay in memory or on that host's
disk. Postgres is the shared transcript when more than one process
needs the same store. Give each process its own SQLite file, or share
Postgres.

With `apipi serve --api-only` and `apipi worker`, session ownership is
the worker lease. API nodes are interchangeable for follow-up REST and
SSE (SSE also polls the store). `self_hosted` runner sockets still
stick to the API process that created them.

Several processes behind a load balancer work if follow-up requests
return to the node that owns the session (sticky affinity). Scale by
adding systemd units rather than uvicorn workers inside one process.

## Topology

N gateway hosts, one Postgres, one load balancer. Each host runs
`apipi serve` under systemd with `APIPI_RUN_MODE=microvm` as in
[run modes](run-modes.md#production). Give each process its own
`APIPI_SESSIONS_DIR`. Point every process at the same `DATABASE_URL`.

Set `APIPI_INSTANCE_ID` to a short name per process (`node-a`). When
set, HTTP responses except `/health` include `X-ApiPi-Instance`. Use
that header to confirm the balancer is sticky. An empty value turns
the header off. Authenticated responses also include `X-Tenant-Id` and
`X-User-Id`. A trusted proxy in front of the balancer may send those
on the request for tenant-pool hashing; the gateway still authenticates
from the bearer only. When a trace is known, responses include
`X-Trace-Id`.

`POST /v1/agents/sessions` may land on any node. After create, the
session id is in the path. Follow-up REST and SSE must return to the
same node.

## Affinity

Official OpenAI clients put `session_id` in
`/v1/agents/sessions/{session_id}/…`. They do not store cookies by
default. Hash that path segment.

`GET /v1/agents/sessions/{id}/events?stream=true` uses the same path.
If SSE drops, reconnect with `after_seq` to replay from the store. The
next turn still needs the node that holds Pi.

`/v1/environments/{environment_id}` is the `self_hosted` runner
WebSocket. That id is not the session id, so a hash on session id will
not send the runner to the same node. For `self_hosted` with more than
one node, use a dedicated tenant pool with one node, or send the
runner to the instance shown in `X-ApiPi-Instance` on create. Cookie
stickiness only works if the client stores cookies; the OpenAI SDK
does not.

Send follow-up turns to the node that already owns the session so Pi
and the workspace are there.

## nginx

Health checks should call `GET /health`. Probe health rather than a
session. Take a node out of the upstream, wait until turns finish or
idle TTL has killed Pi, then stop the unit. Keep health successful
while a turn is in flight.

Long-lived SSE and WebSockets need buffering off and a long read
timeout. One hour matches a long turn plus idle.

```
map $uri $apipi_session {
    ~^/v1/agents/sessions/(?<sid>[0-9a-fA-F-]+) $sid;
    default "";
}

upstream apipi_any {
    least_conn;
    server 192.0.2.10:8000;
    server 192.0.2.11:8000;
}

upstream apipi_session {
    hash $apipi_session consistent;
    server 192.0.2.10:8000;
    server 192.0.2.11:8000;
}

server {
    listen 443 ssl;
    location ~ ^/v1/agents/sessions/[0-9a-fA-F-]+ {
        proxy_pass http://apipi_session;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    location /v1/environments/ {
        proxy_pass http://apipi_any;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
    location / {
        proxy_pass http://apipi_any;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

The `/v1/environments/` location is for a single-node tenant pool or
for a runner you already send to the owning instance. It is not
session-sticky by itself.

## Tenant pools

Some tenants get their own gateway pool: a separate Host name or load
balancer backend group. Auth still returns `tenant_id`. You map that
tenant to the pool outside the gateway (DNS, balancer rule, or which
bearers the auth callback accepts on that pool). The public Agents API
does not change.

Shared: Postgres, and artifact bytes when `APIPI_ARTIFACT_STORE=s3`.
Isolated: Pi and `APIPI_SESSIONS_DIR` (the live workspace). Sticky
rules above still apply inside the pool for live sessions. With local
artifact files, give each node its own sessions directory.
