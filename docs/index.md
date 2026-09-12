# ApiPi

ApiPi is a drop-in OpenAI Agents API. Point official clients at this
gateway and bring your own model URL.

You create an agent, open a session, send messages, and stream events.
A session is a conversation that can use function tools, MCP servers,
and an optional computer. The computer can be a local session directory
or a runner you attach. Pi runs the agent loop behind the HTTP API. You
bring any OpenAI-compatible model endpoint. The package and CLI are
`apipi`. A hosted deploy lives at [geki.ai](https://geki.ai).

This page is the product home: what the gateway is, how to install it,
how to run it, and how to point a client at `/v1`. The pages after it
are the HTTP specs. Contributing, the constitution, and architecture
decisions live under Contribute.

## Status

`apipi serve` starts the FastAPI gateway. Run mode `host` works: Pi is a
child process (`pi --mode rpc`), one process per session. `jail` and
`microvm` are configured names, but they are not implemented. If you
start the process with either of those modes, it exits. There is no
silent fallback.

The configured default for `APIPI_RUN_MODE` is `jail`. Because jail is
not available yet, you must set `APIPI_RUN_MODE=host` to serve. `host`
logs a warning at startup and is not suited for production.

Postgres is required. Live turns need Pi on `PATH` and a model URL.
Tests use a FakeHarness and do not need a live model.

## Install

Python 3.13+ and [uv](https://docs.astral.sh/uv/) only. Do not use pip
or a bare `python -m venv`.

```
uv sync
```

That installs the `apipi` CLI into the project environment. After
`uv sync` you can run `apipi` from that environment, or prefix commands
with `uv run`.

## Setup

The gateway stores tenants, agents, sessions, turns, items, and the
append-only event log in Postgres. It does not store API keys. A Compose
file at the repo root starts a local Postgres 17 server with user
`apipi`, password `apipi`, and database `apipi`, published on host port
5432:

```
docker compose up -d postgres
```

Point the CLI at that database and apply store migrations:

```
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
```

`DATABASE_URL` is required. `postgres://` and `postgresql://` URLs are
rewritten to `postgresql+asyncpg://`. SQLite is for tests only and is
rejected by `apipi serve` and `apipi migrate`.

Pi talks to your model with the usual OpenAI environment variables.
`OPENAI_BASE_URL` is the model host, not this gateway.
`OPENAI_API_KEY` is the key that host expects. Those values are passed
into the Pi process. Pi does not receive `DATABASE_URL` or gateway
secrets.

Live turns also need the Pi CLI (`pi --mode rpc`) on `PATH`. The
gateway pins Pi 0.85.1. You can override the binary with
`APIPI_PI_COMMAND`.

## Run

The configured default run mode is `jail`, but jail is not available
yet. `microvm` is not available either. You must set `host`, or the
process exits:

```
APIPI_RUN_MODE=host apipi serve
```

That binds `0.0.0.0:8000` by default (`--host` and `--port` change the
bind). `host` runs Pi as a child of the gateway. The process logs a
warning: `APIPI_RUN_MODE=host is not suited for production`. Startup
also logs that the turn log is on, and whether Prometheus metrics and
OpenTelemetry export are on.

If the selected run mode cannot start, the process exits. There is no
fallback to another mode.

`GET /health` returns `{"status": "ok"}` and does not require a bearer.

## Use

Point an OpenAI-compatible client at `http://localhost:8000/v1`. Send
`Authorization: Bearer` on every request except `/health` and
`/metrics`. Default auth accepts any non-empty bearer and hashes it
into a tenant id. The same key always maps to the same tenant. See
[auth](auth.md).

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dev-token",
)

agent = client.beta.agents.create(
    name="demo",
    model="gpt-4.1",
    instructions="Be brief.",
)
session = client.beta.agents.sessions.create(
    agent_id=agent.id,
    input="Hello",
)
print(session.id)
```

Official clients work for the subset we implement. Unknown fields and
unimplemented features return an error (`invalid_request` or
`not_implemented`). They are not stored and they are not ignored. A
runnable script that uses the official OpenAI Python SDK is
`examples/openai_sdk.py`. The HTTP surface is in [API](api.md).

## Config

These are the settings the process reads.

| Config | Default | What |
| --- | --- | --- |
| `DATABASE_URL` | required | Postgres URL. `postgresql+asyncpg://…` preferred. |
| `APIPI_RUN_MODE` | `jail` | `host` \| `jail` \| `microvm`. Only `host` is implemented. Serve with `host` or the process exits. |
| `APIPI_IDLE_TTL` | `15m` | Kill an idle Pi process. The session row stays. Resume from the event log. |
| `APIPI_AUTH` | unset (default hash) | Import path `package.mod:func` for the auth callback. |
| `APIPI_AUTH_CACHE_TTL` | `30s` | Cache the callback result by SHA-256 of the bearer, never the raw key. |
| `APIPI_PI_COMMAND` | `pi` | Pi binary used as `pi --mode rpc`. |
| `APIPI_SESSIONS_DIR` | `.apipi/sessions` under cwd | Root for local session directories (`openai_hosted`). |
| `OPENAI_BASE_URL` | unset | Model host passed to Pi. Not the gateway URL. |
| `OPENAI_API_KEY` | unset | Model key passed to Pi. |
| `APIPI_METRICS` | off | Prometheus text at `/metrics` when on. No bearer. |
| `APIPI_OTEL_ENDPOINT` | unset | OTLP/HTTP traces when set. `/v1/traces` is appended if missing. |

A setting that would store prompt or completion bodies is rejected at
startup.

## Read next

Use the API:

1. [API](api.md)
2. [Auth](auth.md)
3. [Environments](environments.md)
4. [Tools and skills](tools.md)
5. [Usage](usage.md)
6. [Architecture](architecture.md)

If you are changing the code, start from [How we work](process.md) and
[Contributing](contributing.md). Project rules are in the
[constitution](constitution.md). What is not in this version is on the
[roadmap](roadmap.md).
