# Auth

`Authorization: Bearer` on every request except `/health`.

We do not mint or store API keys. Callers reuse the bearer they already
use with an LLM router. Isolation is by `tenant_id`. Wrong-tenant IDs
are `404`. Missing or rejected bearer is `401`.

## Callback

Each request:

```
authenticate(bearer) -> {key_id, tenant_id} | reject
```

In-process Python. `APIPI_AUTH` is an import path (`package.mod:func`).
Unset means the default.

Default: any non-empty bearer is accepted. `key_id` is SHA-256 hex of
the bearer. `tenant_id` is UUID5 of that hex. Same key, same tenant.

A plugin returns `tenant_id` and `key_id`, or rejects. `key_id` is for
logs and metrics. Queries stay tenant-scoped.

## Cache

Cache the callback result by SHA-256 of the bearer. Never the raw key.
TTL `APIPI_AUTH_CACHE_TTL`, default `30s`. Plugin errors are not stored
as success.

## Store

A `tenants` row is created on first use of a `tenant_id`. There is no
`api_keys` table and no `apipi tenant create`.
