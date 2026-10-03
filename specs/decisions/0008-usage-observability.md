# 0008. Usage and observability

ApiPi records agent-layer usage: turns, tools, MCP, environment, run
mode, latency, artifact bytes, and turn-level token totals. It does
not own per-LLM-call tracing. That stays on the model API.

Postgres is the hot store. `APIPI_USAGE_STORE` is `turns` (default),
`rollups`, or `off`. Turn log rows may expire (`APIPI_USAGE_RETENTION`,
default 15 days). Daily tenant rollups stay for `GET /v1/apipi/usage?day=`.
Long-term analytics use an optional HTTPS usage export
(`APIPI_USAGE_EXPORT_URL`) plus any extra `emit` sinks on
`APIPI_USAGE_SINKS`. Prompt and tool bodies never go in that
store. The only supported path for those bodies is an optional HTTPS
payload export (`APIPI_PAYLOAD_EXPORT_URL`), off by default, plus
`APIPI_PAYLOAD_SINKS`. Prometheus and OpenTelemetry traces are exports
of the same non-text facts.

Tokens now, dollar cost later. A setting that would store prompt or
completion bodies in ApiPi is rejected.

The worker socket is observed the same way, from both processes. The
shared pieces live in `apipi.common`: one `Metrics` registry per
process, a rate-limited warning logger, a wrapper for background
loops, an event loop lag sampler, and a log context that puts
`worker_id` and `connection_id` on every line of a connection. The
series and log events are listed in `docs/observability.md` and
`docs/usage.md`. Labels stay low-cardinality, and only
`apipi_worker_info` carries a worker id.
