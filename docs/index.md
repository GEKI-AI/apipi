# ApiPi

ApiPi is an open-source agent platform. [Pi](https://pi.dev) is the
agent harness. You run agents at scale in isolated environments, keep
full control of those agents, and point them at **your own** LLM
endpoints. Clients talk to agents through an HTTP interface compatible
with the OpenAI Agents API.

This project is for teams that want to self-host or embed an Agents API
gateway — SaaS builders and enterprise platform groups. It is not a
workflow canvas, a model host, a search engine, or a copy of every
OpenAI Agents object. There is no first-party chat UI, ChatKit, or
`/v1/chat/completions`. Search and browser attach as MCP, not as
built-in tools.

In production, each live session runs in a hardware-virtualized microVM
using [Firecracker](https://firecracker-microvm.github.io/). The guest
has its own kernel, which is the isolation that protects the host from
a hostile session. The gateway process stays on the host. Pi, stdio
MCP, and a local session directory share the guest. How that works, and
what to install, is in [run modes](run-modes.md) and
[production](production.md).

GEKI operates a managed ApiPi on European infrastructure if you want
the same isolation and control without running the hosts yourself. See
[geki.ai](https://geki.ai).

Official OpenAI clients work for the subset we implement. Unknown
fields and unimplemented features return an error (`invalid_request` or
`not_implemented`). They are not stored and they are not ignored.

`environment.type` `openai_hosted` is OpenAI's field name for a **local
session directory** next to Pi. It is not OpenAI's cloud VM.

| You get | You bring |
| --- | --- |
| Agents, sessions, events, artifacts | An OpenAI-compatible model URL |
| Function tools, MCP, skills | A bearer the gateway does not store |
| A local directory or a `self_hosted` runner | Pi on `PATH` for live turns |

## Start

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/) only. Postgres
is required. From a checkout:

```
uv sync
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
APIPI_RUN_MODE=none apipi serve
```

That binds `0.0.0.0:8000`. Isolation `none` is the process default so a
laptop without KVM can start; it is not the production posture.
Production operators set `APIPI_RUN_MODE=microvm`. Full setup is in
[Install](install.md).

Point a client at `http://localhost:8000/v1` with
`Authorization: Bearer`. Default auth accepts any non-empty bearer and
hashes it into a tenant id.

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

A runnable script is `examples/openai_sdk.py`. The same steps as
OpenAI's Agents API quickstart are in [Using the API](using.md). The
HTTP surface is in [API](api.md). Agents, sessions, and files are in
[Concepts](concepts.md). Host sizing and scale-out are in
[production](production.md). More than one gateway process is in
[multiple nodes](scale.md).
