# ApiPi

ApiPi is an open-source agent platform. [Pi](https://pi.dev) is the
agent harness. You run agents at scale in isolated environments, keep
full control of those agents, and point them at **your own** LLM
endpoints. Clients talk to agents through an HTTP interface compatible
with the OpenAI Agents API.

The audience is teams that want to self-host or embed an Agents API
gateway — SaaS builders and enterprise platform groups — not a workflow
canvas and not a model host.

In production, each session runs in a hardware-virtualized microVM
using [Firecracker](https://firecracker-microvm.github.io/). The guest
has its own kernel, which isolates tenants more strongly than a
shared-kernel container. The gateway stays on the host. Pi, stdio MCP,
and the local session computer share that guest. Local development can
skip the sandbox (`APIPI_RUN_MODE=none`); that mode is not for
production.

GEKI operates a managed ApiPi on European infrastructure if you want
the same isolation and control without running the hosts yourself. See
[geki.ai](https://geki.ai).

## Install

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/) only. Do not
use pip or a bare `python -m venv`.

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

The process default is `none`, so `apipi serve` can start on a laptop
without KVM. Pi then runs as a child of the gateway and the process
logs a warning that this is not for production:

```
apipi serve
```

For SaaS or enterprise, set `APIPI_RUN_MODE=microvm`. That boots each
session in a Firecracker guest when `/dev/kvm`, `firecracker`,
`jailer`, guest images, `ip`, and `iptables` are present. If that
cannot start, the process exits. There is no silent fallback:

```
APIPI_RUN_MODE=microvm apipi serve
```

That binds `0.0.0.0:8000` by default. Settings load from environment
variables, optional `.env`, and optional `apipi.toml`. Operator
install, Firecracker images, and systemd are in the docs.

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
