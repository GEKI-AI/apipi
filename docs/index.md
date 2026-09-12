# ApiPi

ApiPi is a drop-in OpenAI Agents API. Point official clients at this
gateway and bring your own model URL.

You create an agent, open a session, send messages, and stream events.
A session is a conversation that can use function tools, MCP servers,
and an optional computer. The computer can be a local session directory
or a runner you attach. Pi runs the agent loop behind the HTTP API. You
bring any OpenAI-compatible model endpoint. The package and CLI are
`apipi`. A hosted deploy lives at [geki.ai](https://geki.ai).

This page is the product home: what the gateway is, how to start it,
and how to point a client at `/v1`. [Install](install.md) and
[configuration](config.md) are the operator pages. The
[quickstart](quickstart.md) is the client tutorial. The pages after
those are the HTTP specs. Contributing, the constitution, and
architecture decisions live under Contribute.

## Status

`apipi serve` starts the FastAPI gateway. Run mode `host` works: Pi is a
child process (`pi --mode rpc`), one process per session. Run mode
`jail` starts Pi (and stdio MCP) in a Linux namespace jail when
`bwrap`, `pasta`, and cgroup v2 can start. If those tools are missing,
the process exits. Run mode `microvm` starts Pi (and stdio MCP) in a
Firecracker guest when `/dev/kvm`, `firecracker`, `jailer`, the
kernel and rootfs images, `ip`, and `iptables` are present. If those
are missing, the process exits. There is no silent fallback.

The configured default for `APIPI_RUN_MODE` is `jail`. Operators
without jail tools must set `APIPI_RUN_MODE=host`. `host` logs a
warning at startup and is not suited for production.

Postgres is required. Live turns need Pi on `PATH` and a model URL.
Tests use a FakeHarness and do not need a live model.

## Install and run

Python 3.13+ and [uv](https://docs.astral.sh/uv/) only. Postgres is
required. From a checkout:

```
uv sync
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
APIPI_RUN_MODE=host apipi serve
```

That binds `0.0.0.0:8000`. `jail` is the configured default when
`bwrap`, `pasta`, and cgroup v2 can start. Operators without those
tools must set `host`. The full install, systemd, and run-mode notes
are in [Install](install.md). Every setting is in [Configuration](config.md).

## Use

Point an OpenAI-compatible client at `http://localhost:8000/v1`. Send
`Authorization: Bearer` on every request except `/health` and
`/metrics`. Default auth accepts any non-empty bearer and hashes it
into a tenant id. The same key always maps to the same tenant. See
[auth](auth.md).

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
unimplemented features return an error (`invalid_request` or
`not_implemented`). They are not stored and they are not ignored. A
runnable script is `examples/openai_sdk.py`. The same steps as OpenAI's
Agents API quickstart are in [Quickstart](quickstart.md). The HTTP
surface is in [API](api.md).

## Read next

Use the API:

1. [Install](install.md)
2. [Configuration](config.md)
3. [Quickstart](quickstart.md)
4. [API](api.md)
5. [Auth](auth.md)
6. [Environments](environments.md)
7. [Tools and skills](tools.md)
8. [Usage](usage.md)
9. [Architecture](architecture.md)

If you are changing the code, start from [How we work](process.md) and
[Contributing](contributing.md). Project rules are in the
[constitution](constitution.md). What is not in this version is on the
[roadmap](roadmap.md).
