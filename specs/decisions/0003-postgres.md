# 0003. The durable store is the source of truth

The relational store holds sessions, items, and the append-only event
log. Items are conversation objects. The event log is the ordered
public stream. Token fragments (`output_text.delta`) are live SSE
only; they are not stored. Reconnect and export use `output_text.done`
and items for assistant text. Pi's on-disk session is a cache. The API
reads the store. Postgres is the production store. SQLite is the local
single-process default when `DATABASE_URL` is unset.
