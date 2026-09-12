# Auth

`Authorization: Bearer` is required on every request except `/health`
and `/metrics`. Missing, empty, or rejected bearer is `401`.

The gateway does not mint or store API keys. Callers reuse the bearer
they already use with an LLM router. Isolation is by `tenant_id`. An
id that belongs to another tenant is `404`, not `403`. Browsers must
not hold tenant keys. There is no first-party UI that would need a
cookie.

## Callback

Each request:

```
authenticate(bearer) -> {key_id, tenant_id} | reject
```

The callback is in-process Python. `APIPI_AUTH` is an import path
(`package.mod:func`). Unset means the default function in this package.

Default: any non-empty bearer is accepted. `key_id` is the SHA-256 hex
of the bearer. `tenant_id` is UUID5 of that hex (URL namespace). The
same key always maps to the same tenant. Different keys are different
tenants.

A plugin returns `tenant_id` and `key_id`, or rejects (returns `None`
or raises). `key_id` is for logs and metrics. Queries stay
tenant-scoped. The plugin must not expect the gateway to persist the
raw bearer.

## Cache

The gateway caches the callback result by SHA-256 of the bearer. It
never caches the raw key. TTL is `APIPI_AUTH_CACHE_TTL`, default
`30s`. A reject is cached so a bad key is not retried on every
request. Plugin errors are not stored as success; the next request
calls the plugin again.

## Store

A `tenants` row is created on first use of a `tenant_id`. There is no
`api_keys` table and no `apipi tenant create`. Postgres holds tenants,
sessions, and the event log. It does not hold secrets.
