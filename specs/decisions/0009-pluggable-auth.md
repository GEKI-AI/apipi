# 0009. Pluggable auth, no stored keys

Auth is a callback, not a key table. This process sits next to an LLM
router and accepts the same bearer.

Default: hash the bearer for `key_id`, UUID5 of that hash for
`tenant_id`, accept every non-empty key. A short TTL cache (default
30s) is keyed by the hash so an upstream plugin is not hit on every
request.

Postgres keeps tenants, sessions, and the event log. It does not keep
secrets.
