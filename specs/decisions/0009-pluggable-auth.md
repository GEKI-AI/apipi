# 0009. Pluggable auth, no stored keys

Auth is a callback, not a key table. This process sits next to an LLM
router and accepts the same bearer.

Default: hash the bearer for `key_id`, UUID5 of that hash for
`tenant_id`, accept every non-empty key. A short TTL cache (default
30s) is keyed by the hash so an upstream plugin is not hit on every
request.

The plugin may reject with HTTP status, public `code`, and `message`.
Invalid keys are `401` `unauthorized`. Rate limits and auth-level
quotas are `429` with a plugin `code`. Success and `401` rejects are
cached as themselves. `429` is not cached. Unexpected plugin errors
stay `401` and are not cached as success.

Postgres keeps tenants, sessions, and the event log. It does not keep
the gateway auth bearer. MCP vault tokens may be stored tenant-scoped;
GET never returns them. See 0011.
