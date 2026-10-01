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
authenticate(bearer, request) -> {key_id, tenant_id, user_id?, org_id?, cache_key?} | reject
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
is the end user when the plugin knows it. Optional `org_id` is the
billing org when the plugin knows it. ApiPi does not invent
`user_id` or `org_id` from `key_id`. `key_id` is for logs. The plugin must not
expect the gateway to persist the raw bearer. After a successful
callback, HTTP responses include `X-Tenant-Id` and `X-User-Id`
(`key_id`, not `user_id`). Incoming values of those headers are not
trusted for auth. Usage events and the usage export include `user_id`
when the plugin set it.

A one-argument `authenticate(bearer)` plugin still works. A
two-argument plugin also receives `AuthRequest`: `method`, `path`
(no query string), and `headers`. Header names are lowercase.
`authorization` is omitted because the bearer is the first argument.
Those headers are client-supplied. The gateway does not treat
`x-tenant-id`, `x-user-id`, or any other header as identity. The
plugin decides which header, if any, names the end user.

When the identity includes `user_id`, session create stores it. List,
get, update, delete, resume, export, turns, items, and artifacts then
match tenant and `user_id`. A missing session for that user is `404`.
When the identity includes `org_id`, session create stores it and
returns it on the session. `org_id` does not change which sessions a
caller can see. Lifecycle export forwards it to the pool owner. See
[usage](usage.md#session-lifecycle-export).
An identity without `user_id` stays tenant-scoped, as before. Agents
stay tenant-scoped. Usage by `day` stays tenant-scoped. Usage by
`session_id` or `turn_id` uses the same session rule.

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

The gateway caches the callback result by SHA-256 of a cache key. It
never caches the raw key. The default cache key is the bearer, so
existing plugins keep one entry per token. A plugin that serves many
end users on one bearer must not share that entry.

Set `authenticate.cache_key` to a callable with the same shape as
`authenticate`, or define `cache_key` next to the `APIPI_AUTH`
function. It may return a non-empty string or `None`. A string is
hashed and used for lookup. `None` falls back to the bearer hash.
The identity or reject may also include `cache_key`. That string is
the storage key. If it differs from the lookup key, the gateway
stores the result only under the result key, so another user on the
same bearer does not reuse it. Cache hits for that user require the
`cache_key` callable to return the same string on the next request.
Do not put the raw bearer in `cache_key` if you can avoid it. The
value stays in process memory for the TTL. It is not written to
Postgres.

TTL is `APIPI_AUTH_CACHE_TTL`, default `30s`. A successful identity
is cached. A `401` reject is cached as that typed reject, so a bad
key is not retried on every request and a limit is not stored as
success. A `429` reject is not cached, so a quota can recover before
the TTL. Plugin errors are not stored as success; the next request
calls the plugin again.

The cache is a bounded LRU with `APIPI_AUTH_CACHE_MAX` entries
(default `10000`). Over the limit the least-recently-used entry is
evicted. `0` disables caching entirely: every request calls the
plugin, which is useful for tests and strict revocation.

The plugin may be `async def`, or a sync function that returns an
awaitable. Otherwise it runs in a worker thread, so sync plugins that
do network or DB I/O never block the event loop. Concurrent misses
for the same cache key call the plugin once (single-flight).

`ensure_tenant` is memoized per `tenant_id` in the gateway with the
same bound, so an auth plus tenant cache hit makes no plugin call and
no tenant lookup. Tenants are never deleted, so the memo needs no
TTL.

## Invalidation

Per process. With several gateway instances, call every instance or
rely on the TTL.

In process:

```python
gateway.invalidate_auth("the-bearer-or-cache-key")  # -> bool
gateway.invalidate_auth_where(lambda i: i.user_id == "u1")  # -> int
gateway.clear_auth_cache()  # -> int
```

`invalidate_auth` takes the unhashed cache key (the bearer by
default, or the plugin's `cache_key` value) and hashes it the same
way as lookup. `invalidate_auth_where` drops every cached identity
that matches; cached rejects are left alone. `clear_auth_cache`
drops everything and returns the count.

Over HTTP, `POST /v1/apipi/auth/invalidate` is authenticated like
every other route. The body filters are optional and combined with
AND: `{"key_id": "...", "user_id": "...", "org_id": "..."}`. An
empty body means every cached identity of the caller's tenant. The
route is always scoped to the caller's tenant and cannot drop
another tenant's entries. Cross-tenant invalidation is in-process
only. When the hook below is configured it runs with action
`auth.invalidate`. The response is `{"invalidated": <count>}`.

## Authorization hook

Optional. `APIPI_AUTHORIZE` (TOML `authorize`) is an import path
(`package.mod:func`), or pass `authorize=` on `Gateway.create`. It
may be sync (run in a thread) or async.

```python
authorize(identity, action, resource_type, resource_id, ctx=None) -> None | AuthReject | AuthFilter
```

`identity` is the cached `AuthIdentity`. `action` is one of the
stable names below. `resource_type` is `agent`, `vault`, `file`,
`skill`, `template`, `usage`, or `auth`. `resource_id` is the id
string, or `None` on create. `ctx` is the `AuthRequest` (method,
path, headers) when the plugin takes five arguments.

Return `None` to allow. Return `AuthReject` (default `403` with code
`forbidden` for this path) to deny. Return `AuthFilter(ids)` only
for list actions; `None` means all. The service applies the filter
so pagination and counts stay correct.

The hook runs after the gateway resolves the target resource and
before any side effect. For session routes it receives the agent id
of the loaded session, so "this key may only use agent X" works on
every session sub-route. For session create the agent id comes from
the body; inline agents pass `None`. A resource that does not exist
stays `404`; the hook runs only for resources that exist.

| Action | Routes (resource type → id) |
|---|---|
| `agent.read` | `GET /v1/agents/{id}`, `GET /v1/apipi/agents/{id}/export` (agent) |
| `agent.write` | `POST /v1/agents` (id `None`), `POST`/`DELETE /v1/agents/{id}`, `POST /v1/apipi/templates/{id}/agents` (agent) |
| `agent.list` | `GET /v1/agents` (list → `AuthFilter`) |
| `agent.run` | `POST /v1/agents/sessions`, `POST /v1/agents/sessions/{id}`, `DELETE …/sessions/{id}`, `POST …/sessions/{id}/events`, artifact delete (agent of the session) |
| `session.read` | `GET …/sessions/{id}`, `…/events`, `…/turns[/{t}]`, `…/items`, `…/export`, `…/artifacts[...]` (resource type `agent`, agent of the session) |
| `session.list` | `GET /v1/agents/sessions` (list → `AuthFilter` on agent ids) |
| `vault.read` / `vault.write` / `vault.list` | `/v1/agents/vaults[/{id}]` and `…/credentials[...]` (vault) |
| `file.read` / `file.write` / `file.list` | `/v1/files[...]`, `/v1/apipi/uploads[...]` (file; `None` on create) |
| `skill.read` / `skill.write` / `skill.list` | `/v1/skills[...]` (skill) |
| `template.read` / `template.write` / `template.list` | `/v1/apipi/templates[...]` (template) |
| `usage.read` | `GET /v1/apipi/usage` |
| `auth.invalidate` | `POST /v1/apipi/auth/invalidate` |

Chat routes map to the same `agent.run`, `session.read` and
`session.list` actions. Model listing and health are not authorized
by the hook. Without a hook everything behaves as before.

## Store

A `tenants` row is created on first use of a `tenant_id`. There is no
`api_keys` table and no `apipi tenant create`. Postgres holds tenants,
sessions, and the event log. It does not hold the gateway auth bearer.
MCP vault tokens may be stored tenant-scoped. They are encrypted at
rest with AES-256-GCM (`APIPI_VAULT_MASTER_KEY`). GET never returns
those token values. Guests and browsers never see them. See
[configuration](config.md).
