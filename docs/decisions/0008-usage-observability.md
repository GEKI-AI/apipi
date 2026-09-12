# 0008. Usage and observability

Postgres is the source of truth for usage. Prometheus and OpenTelemetry
are exports.

Tokens now, dollar cost later. Every turn writes a log row with totals
and details. Never store prompt or completion text.

`APIPI_METRICS` (off) and `APIPI_OTEL_ENDPOINT` (unset) turn exports on.
The turn log is always on. A setting that would store prompt or
completion bodies is rejected.
