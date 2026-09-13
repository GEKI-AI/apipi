# 0008. Usage and observability

ApiPi records agent-layer usage: turns, tools, MCP, environment, run
mode, latency, artifact bytes, and turn-level token totals. It does
not own per-LLM-call tracing. That stays on the model API.

Postgres is the hot store. `APIPI_USAGE_STORE` is `turns` (default),
`rollups`, or `off`. Turn log rows may expire (`APIPI_USAGE_RETENTION`,
default 15 days). Daily tenant rollups stay for `GET /v1/usage?day=`.
Long-term analytics use an optional HTTPS usage export
(`APIPI_USAGE_EXPORT_URL`). Prometheus and OpenTelemetry traces are
exports of the same non-text facts.

Tokens now, dollar cost later. Never store prompt or completion text.
A setting that would store those bodies is rejected.
