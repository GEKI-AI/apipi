# How it works

The explanation of the system is under [Concepts](concepts.md):
[how it fits together](concepts.md), [isolation](isolation.md), and
[workers](worker-concepts.md). This page is a short map.

```
  OpenAI SDK / your app
           |
           |  bearer key
           v
      FastAPI gateway             never in the guest
      store (SQLite or Postgres)
           |
           |  in-process or worker lease
           v
      Pi  (+ stdio MCP)           none | microvm
           |
           +-- local files        next to Pi
           +-- or remote env      self_hosted runner
           +-- HTTP MCP           e.g. Tavily
```

| | What it controls |
| --- | --- |
| **Run mode** | Where Pi (and stdio MCP) run |
| **Environment** | Where file/shell tools run |

Guest images are prebuilt files. A worker pulls them from an
`s3://`, `https://`, or `file://` source before it starts. The session
picks an image id. Placement uses only a worker that has that image.
The store format is
[ADR 0012](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0012-guest-image-store.md).

HTTP routes do not spawn Pi themselves. They call `SessionService`,
which sits above the session execution adapter. Extenders use the same
service in-process (`gateway.sessions`), including `stream()` for
catch-up from the store plus live EventHub events (and a store poll,
the same as SSE). Combined `apipi serve` uses the in-process adapter.
`apipi serve --api-only` leases a worker. Firecracker stays on the
worker, or on combined serve as an embedded worker. EventHub is per
API process; `stream()` is not a multi-replica bus.

To run the Agents API and your own routes in one process, build a
`Gateway` with `Gateway.create`, call `startup` and `shutdown` from
your FastAPI lifespan, call `configure` so `app.state`, middleware, and
exception handlers are installed, then `include_router` for each
`gateway.routers.*` you want. `apipi serve` does that wiring for
standalone. `startup` attaches the store and starts usage and
worker-lease expiry loops. Combined serve also starts idle-Pi and
hosted workspace reap. In a split deploy those two loops run on
`apipi worker`; the API copies are no-ops. Pass `extend_settings(...)` so host
`DATABASE_URL` and `OPENAI_*` values do not leak in. Pass your `Store` if
you own the engine; Gateway does not dispose an injected store. Pass
`authenticate=` to inject the auth callback without `APIPI_AUTH`. Mounting
`create_app()` under a path does not run its lifespan; call `startup` on
the host app. If the Agents API is not at the domain root, `APIPI_API_URL`
for workers must include that prefix. The full page is
[Extending ApiPi](extending.md).

The durable store holds tenants, agents, sessions, turns, items, the
event log, usage (never prompt text), and artifact metadata. Artifact
bytes sit in the configured object store (local or S3). Hosted file and
skill bytes use the same backends. Pi JSONL is a cache in
that same blob store; the session row keeps a pointer, not the file.
The event log is the transcript. Cross-tenant IDs return `404`, not
`403`. SQLite is one process. Postgres is shared.

Auth is a callback on the bearer. See [auth](auth.md). Tools, MCP, and
skills are in [tools](tools.md). Environments are in
[environments](environments.md). Operator install is in
[install](install.md).
