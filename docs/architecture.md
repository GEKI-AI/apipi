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

HTTP routes do not spawn Pi themselves. They call a session execution
service. Combined `apipi serve` uses the in-process adapter. `apipi
serve --api-only` leases a worker. Firecracker stays on the worker, or
on combined serve as an embedded worker.

The durable store holds tenants, agents, sessions, turns, items, the
event log, usage (never prompt text), and artifact metadata. Artifact
bytes sit in the configured artifact store. Pi JSONL is a cache.
Cross-tenant IDs return `404`, not `403`. SQLite is one process.
Postgres is shared.

Auth is a callback on the bearer. See [auth](auth.md). Tools, MCP, and
skills are in [tools](tools.md). Environments are in
[environments](environments.md). Operator install is in
[install](install.md).
