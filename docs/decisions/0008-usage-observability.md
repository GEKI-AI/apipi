# 0008. Usage and observability

Postgres is the source of truth for usage. Prometheus and OpenTelemetry
are exports.

Tokens now, dollar cost later. Every turn writes a log row with totals
and details. Never store prompt or completion text.
