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

The configured default run mode is `jail`. That starts Pi in a Linux
namespace jail when `bwrap`, `pasta`, and cgroup v2 are present:

```
apipi serve
```

If those tools are missing, the process exits. Operators without jail
tools must set `host`. `microvm` starts Pi in a Firecracker guest when
`/dev/kvm`, `firecracker`, `jailer`, guest images, `ip`, and
`iptables` are present. Otherwise that mode exits too:

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

with OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dev-token",
) as client:
    with client.beta.agents.sessions.with_streaming_response.create(
        agent={
            "model": "gpt-4.1",
            "instructions": "Write clean code, run it, and report the actual output.",
        },
        environment={"type": "openai_hosted"},
        input="Create tree.py, run it, and show me the output.",
        stream=True,
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                print(line.removeprefix("data: "), flush=True)
```

Official clients work for the subset we implement. Unknown fields and
missing features fail clearly. A runnable script is
[examples/openai_sdk.py](examples/openai_sdk.py). The same steps as
OpenAI's Agents API quickstart are in the docs.

## Docs and contributing

The MkDocs site is the product documentation. It is not part of the
`apipi` package:

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

To change the code, start from [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
