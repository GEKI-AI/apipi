# Extending ApiPi

You extend ApiPi and run **one service**: the OpenAI Agents HTTP API plus
your own routes in the same process. Import `apipi`, construct a
`Gateway`, register the Agents routers you want, and call sessions and
live events in-process. Do not treat ApiPi as a black-box library you
mount and forget.

Wiring stays explicit. There is no `attach_everything(app)` helper.
`apipi serve` remains the standalone CLI; it does this same wiring for
operators. Copy the verbose pattern when you extend.

A minimal ASGI skeleton is `examples/extend_fastapi.py`. A concrete
extension that fetches a page with bash and streams plain text is
`examples/webpage-check/`.

## Extending vs standalone

| How you run | What you do |
| --- | --- |
| Standalone | `apipi migrate` then `apipi serve`. The CLI loads settings from the environment, `.env`, and TOML, builds a `Gateway`, and serves every router including `/health`. |
| Extended | Your process owns the FastAPI app. You build settings with `extend_settings(...)`, optionally inject a `Store`, construct `Gateway.create`, call `startup` / `shutdown` from **your** lifespan, call `configure`, then `include_router` for each ApiPi router you want, then add your routes. |

Both are one HTTP service from the client's point of view. Official
OpenAI Agents clients still talk to `/v1`. Your extra endpoints live
next to that API.

Do not `app.mount("/apipi", create_app())` and expect it to work.
Starlette `Mount` forwards HTTP only. The child lifespan does not run,
so reap loops, store attach, and execution never start. Call
`await gateway.startup()` on the **host** lifespan. If you still put
the Agents API under a path prefix, `APIPI_API_URL` for workers must
include that prefix so `/internal/worker` connects.

## Workers stay vanilla

Workers are unchanged. They run `apipi worker` and speak the existing
control protocol. Your extended service is the API process. Production
split is `api_only` on the API (set it on `extend_settings`) plus
vanilla workers with `APIPI_RUN_MODE=microvm`. Combined in-process
execution is `api_only` off, the same as `apipi serve` without
`--api-only`.

Do not put channel products (Slack, Teams, bot CRUD) in ApiPi. Those
are your routes and your tables.

## Explicit quick start

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apipi.config import extend_settings
from apipi.gateway import Gateway
from apipi.store.engine import Store, create_engine

settings = extend_settings(
    database_url="sqlite+aiosqlite:///.apipi/apipi.db",
    run_mode="none",
)
store = Store(create_engine(settings.database_url))
gateway = Gateway.create(settings, store=store)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await gateway.startup()
    try:
        yield
    finally:
        await gateway.shutdown()


app = FastAPI(lifespan=lifespan)
gateway.configure(app)
app.include_router(gateway.routers.sessions)
app.include_router(gateway.routers.vaults)
app.include_router(gateway.routers.files)
app.include_router(gateway.routers.skills)
app.include_router(gateway.routers.agents)
app.include_router(gateway.routers.environments)
app.include_router(gateway.routers.usage)
app.include_router(gateway.routers.models)
app.include_router(gateway.routers.workers)
app.include_router(gateway.routers.health)
```

`configure(app)` copies gateway state onto `app.state`, installs
middleware, and registers exception handlers. It does **not** start
background tasks and it does **not** register routes. You list
`include_router` yourself so you can skip `/health` or `/internal/worker`.

`startup()` does this:

- `execution.attach_store`
- idle Pi reap loop
- hosted workspace reap loop
- usage log purge loop
- worker-lease expiry loop

`shutdown()` cancels those loops, closes execution, shuts tracing if
ApiPi created it, and disposes the store **only if Gateway created
that store**.

`create_app()` is the thin standalone wrapper that does configure,
every `include_router`, optional `/metrics`, and the lifespan above.
Extenders copy the verbose form instead of calling `create_app()` if
they need to own the FastAPI app.

Run `apipi migrate` against the same database URL before the API
listens. ApiPi will not create tables at `Gateway.create`.

Use `extend_settings(...)` so host `DATABASE_URL` and `OPENAI_*` values
do not leak into ApiPi. `load_settings()` remains the CLI path and
still reads the process environment. See [config](config.md).

Pass `authenticate=` on `Gateway.create` to inject the auth callback
without `APIPI_AUTH`. The callable is the same shape as the plugin in
[auth](auth.md).

## In-process SessionService

HTTP adapters call `SessionService`. Extenders call the same object at
`gateway.sessions`. You do not HTTP-loopback to yourself.

Documented methods:

| Method | What |
| --- | --- |
| `create` | Create a session, optionally start a turn |
| `get` / `list` / `update` / `delete` | Session CRUD |
| `post_event` | Follow-up message, cancel, or tool result |
| `stream(tenant_id, session_id, *, after_seq=None)` | Async iterator of event dicts |

`stream()` matches SSE **event** semantics: replay from the durable
log after `after_seq`, then live EventHub payloads, and a short store
poll when the hub is quiet (the same poll SSE uses for `api_only`
cross-process). SSE comment pings (`: ping`) are HTTP-only; the
iterator does not yield them.

You still authenticate and map a bearer to `tenant_id` yourself when
you call the service from your own route. HTTP routes use
`require_tenant`. In-process callers should
`await gateway.ensure_tenant(tenant_id)` before `create`. Inline agents
on `sessions.create` use `AgentWrite` from `apipi.agents`. Do not import
`apipi.api` or `store.repo` for product functions; `apipi.api` is HTTP
only.

## Lifespan and store ownership

If you pass `store=` into `Gateway.create`, you own the engine.
Gateway will not dispose it on shutdown. Share one SQLAlchemy
`AsyncEngine` with your extension tables.

If you omit `store`, Gateway creates an engine from
`settings.database_url` and disposes it on shutdown.

Call `startup` once from the host lifespan. Mounting `create_app()`
under a prefix does not run that lifespan.

## Database and migrations

Treat ApiPi like a Django app: ApiPi is the core app (its Alembic
tree). Your extension is a second app (its own Alembic tree). One
database.

| Pattern | Guidance |
| --- | --- |
| Shared engine | Same SQLAlchemy `AsyncEngine` / pool. Inject `Store(engine)` into `Gateway.create`. |
| ApiPi migrations | Only via `apipi migrate`. Never `ALTER` ApiPi tables from extension migrations. |
| Extension migrations | Own `DeclarativeBase`, own Alembic `script_location`, **separate `version_table`** (for example `alembic_version_app`). |
| Foreign keys | `ForeignKey("sessions.id")` / `ForeignKey("tenants.id")` by table name is fine. Treat ApiPi ORM models as read-only. Do not subclass them to change columns. |
| Deploy order | Run ApiPi migrations first, then extension migrations, because FKs need parent tables. |
| Autogenerate | Only against extension metadata. Never merge ApiPi `Base.metadata` into app autogenerate. |
| Optional | A Postgres schema `app` versus `public`. A host `migrate-all` script is yours, not ApiPi magic. |

Illustration only (not shipped as an ApiPi table):

```python
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class AppBase(DeclarativeBase):
    pass

class Job(AppBase):
    __tablename__ = "jobs"
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
```

```text
alembic.ini (extension)  →  version_table = alembic_version_app
apipi migrate            →  alembic_version
```

Product-specific models and migrations live in the extending service,
not in ApiPi.

Pitfalls:

- Two version tables are required so revision graphs do not collide.
- An ApiPi upgrade can break your FKs. Pin the `geki-apipi` version.
- `ON DELETE CASCADE` only from extension tables toward ApiPi tables,
  not the other way.
- One engine can commit across both metadata sets in a single
  transaction.
- SQLite is fine for one-process demos. Enable `PRAGMA foreign_keys`
  (ApiPi's `create_engine` already does). Postgres is the shared-store
  choice.
- EventHub is still per API process even when the database is shared.

## Limits

EventHub is in-memory in one API process. `SessionService.stream()`
uses that hub plus a store poll. Several API processes share Postgres;
they do not share the hub. See [multiple nodes](scale.md) for sticky
routing on combined serve versus interchangeable replicas with
`--api-only` and workers.

`/health` and `/metrics` skip request-id and instance headers. If you
serve those paths under a prefix, the skip logic uses `root_path`.
Your own `/health` can replace `gateway.routers.health` by simply not
including that router.

OpenAPI is whatever FastAPI merges from the routers you included.

## Public Python API

Supported for extenders (also listed on `apipi.__all__`):

| Export | Role |
| --- | --- |
| `Gateway` | `create`, `configure`, `startup`, `shutdown`, `ensure_tenant`, `sessions`, `agents`, `vaults`, `usage`, `models`, `routers`, `store`, `event_hub`, `execution`, `workers`, `env_hub`, `authenticate`, `settings` |
| `create_app` | Standalone FastAPI app (CLI and tests) |
| `extend_settings` | `Settings` from arguments only; no env bleed |
| `Settings` | Operator settings type |
| `Store` | Durable store around an `AsyncEngine` |
| `SessionService` | In-process session CRUD, `post_event`, `stream` (`gateway.sessions`) |
| `AgentWrite` | Inline or saved-agent write body. Import from `apipi.agents`. |

`gateway.agents`, `gateway.vaults`, `gateway.usage`, and `gateway.models`
are the same functions as `/v1/agents`, `/v1/agents/vaults`, `/v1/usage`,
and `/v1/models`. Vault get/list never returns credential token values.
Worker and environment WebSockets stay `gateway.workers` and
`gateway.env_hub`.
| `EventHub` | In-process live events |
| `Authenticate`, `AuthIdentity`, `AuthReject` | Auth callback types |

`gateway.routers` names: `sessions`, `agents`, `vaults`,
`environments`, `usage`, `models`, `workers`, `health`.

Everything else under `apipi` is internal unless a product page says
otherwise.

## Testing

Tests in this repo inject a `Store` and `FakeHarness` into
`Gateway.create` / `create_app`, then use httpx `ASGITransport`. Do
the same: do not hit a live network for unit tests, and do not let
`Settings()` read the host `DATABASE_URL`. Use `extend_settings` in
your tests when the process environment is not yours.

## Non-goals

- Channel or bot adapters in ApiPi core
- A magic one-liner that hides lifespan, middleware, and routes
- A multi-app Alembic orchestrator inside ApiPi
- Changing ApiPi's migration system so two products share one
  `alembic_version` table
- First-party agent WebSocket routes (add those in your service with
  `stream()`)
- Multi-process EventHub fanout (Redis and similar)

How the gateway fits together is in [architecture](architecture.md).
Operator install is in [install](install.md).
