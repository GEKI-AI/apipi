# Auth

`Authorization: Bearer` is required on every request except `/health`
and `/metrics`. Missing or empty bearer is `401` with code
`unauthorized`.

The gateway does not mint or store API keys. Callers reuse the bearer
they already use with an LLM router. Isolation is by `tenant_id`. An
id that belongs to another tenant is `404`, not `403`.

The same bearer is the model key unless `OPENAI_API_KEY_OVERWRITE` is
set. Auth only maps the token to `key_id` and `tenant_id`. The raw
gateway bearer is not written to Postgres. Pi reaches the model host
through a host credential broker. The guest does not receive the real
key.

## Callback

Each request:

```
authenticate(bearer) -> {key_id, tenant_id} | reject
```

The callback is in-process Python. `APIPI_AUTH` (TOML `auth`) is an
import path (`package.mod:func`). Unset means the default function in
this package. You can set it in the environment, `.env`, or
`apipi.toml`. See [config](config.md). A small example is
`examples/auth_callback.py`. Isolation backends use the same import
style (`package.mod:Class` on `APIPI_RUN_MODE`); see
[run modes](run-modes.md#custom-isolation). When you construct a
`Gateway` in process, pass `authenticate=` (the same callable). That
does not use `APIPI_AUTH`.

Default: any non-empty bearer is accepted. `key_id` is the SHA-256 hex
of the bearer. `tenant_id` is `tenant_from_key(bearer)`: UUID5 of that
hex (URL namespace). The same key always maps to the same tenant.
Different keys are different tenants. Import `tenant_from_key` from
`apipi` when you already have a key and are not going through HTTP
auth, then `await gateway.ensure_tenant(tenant_id)` before
`sessions.create`.

A plugin returns `tenant_id` and `key_id`, or a typed reject. It may
set its own `tenant_id` (many keys to one tenant). Optional `user_id`
is the SaaS user when the plugin knows it. ApiPi does not invent
`user_id` from `key_id`. `key_id` is for logs. Queries stay
tenant-scoped. The plugin must not expect the gateway to persist the
raw bearer. After a successful callback, HTTP responses include
`X-Tenant-Id` and `X-User-Id` (`key_id`, not `user_id`). Incoming
values of those headers are not trusted for auth. Usage events and
the usage export include `user_id` when the plugin set it.

### Reject

Return `None` for a generic invalid key (`401`, code `unauthorized`).
For a structured error, return `AuthReject` (or a dict) with HTTP
status, public `code`, and `message`. The JSON body is the usual
`error.type` / `error.code` / `error.message` shape. `type` stays
`invalid_request` unless the plugin sets another value.

| Situation | Status | Example `code` |
| --- | --- | --- |
| Invalid or expired key | `401` | `unauthorized` |
| Rate limit or plan cap | `429` | `rate_limited` |
| Tenant quota (agents, seats) | `429` | `quota` |

Live Pi caps on this node remain a separate `429` with code `capacity`
or `capacity_tenant` at turn start. Auth-level limits belong in the
plugin so the client sees the plugin's `code` and `message`. Billing
stays in the plugin.

Unexpected exceptions from the plugin become `401` `unauthorized` and
are not cached.

## Cache

The gateway caches the callback result by SHA-256 of the bearer. It
never caches the raw key. TTL is `APIPI_AUTH_CACHE_TTL`, default
`30s`. A successful identity is cached. A `401` reject is cached as
that typed reject, so a bad key is not retried on every request and a
limit is not stored as success. A `429` reject is not cached, so a
quota can recover before the TTL. Plugin errors are not stored as
success; the next request calls the plugin again.

## Store

A `tenants` row is created on first use of a `tenant_id`. There is no
`api_keys` table and no `apipi tenant create`. Postgres holds tenants,
sessions, and the event log. It does not hold the gateway auth bearer.
MCP vault tokens may be stored tenant-scoped. They are encrypted at
rest with AES-256-GCM (`APIPI_VAULT_MASTER_KEY`). GET never returns
those token values. Guests and browsers never see them. See
[configuration](config.md).
