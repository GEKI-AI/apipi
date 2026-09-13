# 0003. Postgres is the source of truth

Postgres holds sessions, items, and the append-only event log. Pi's
on-disk session is a cache. The API reads Postgres.
