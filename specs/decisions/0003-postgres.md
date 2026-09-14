# 0003. The durable store is the source of truth

The relational store holds sessions, items, and the append-only event
log. Pi's on-disk session is a cache. The API reads the store.
Postgres is the production store. SQLite is the local single-process
default when `DATABASE_URL` is unset.
