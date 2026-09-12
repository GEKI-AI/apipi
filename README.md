# ApiPi

ApiPi is a drop-in OpenAI Agents API. Point official clients at this
gateway and bring your own model URL.

You create agents and sessions over HTTP. A session is a conversation
that can use function tools, MCP servers, and an optional computer
(local files or a runner you attach). Pi runs the agent loop. You bring
any OpenAI-compatible model endpoint. The package and CLI are `apipi`.
A hosted deploy lives at [geki.ai](https://geki.ai).

## Install

Python 3.13+ and [uv](https://docs.astral.sh/uv/) only. Do not use pip
or a bare `python -m venv`.

```
uv sync
```

That installs the `apipi` CLI into the project environment.

## Setup

The gateway stores tenants, agents, sessions, and the event log in
Postgres. A Compose file in this repo starts a local server:

```
docker compose up -d postgres
```

Point the CLI at it, then apply store migrations:

```
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
```

`DATABASE_URL` is required. `apipi migrate` will not start without it.

Pi talks to your model with the usual OpenAI environment variables.
`OPENAI_BASE_URL` is the model host, not this gateway.
`OPENAI_API_KEY` is the key that host expects. Live turns also need the
Pi CLI (`pi --mode rpc`) on `PATH`.

## Run

The configured default run mode is `jail`, but jail is not available
yet. `microvm` is not available either. You must set `host`, or the
process exits:

```
APIPI_RUN_MODE=host apipi serve
```

That binds `0.0.0.0:8000` by default. `host` runs Pi as a child of the
gateway. The process logs a warning: it is not suited for production.
There is no silent fallback to another mode.

## Use

Point an OpenAI-compatible client at `http://localhost:8000/v1`. Send
`Authorization: Bearer` on every request except `/health` and
`/metrics`. Default auth accepts any non-empty bearer and hashes it
into a tenant id. The same key always maps to the same tenant.

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
missing features fail clearly. The full HTTP surface is in the docs.

## Docs and contributing

The MkDocs site is the product documentation. It is not part of the
`apipi` package:

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

To change the code, start from [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
