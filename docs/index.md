# ApiPi

ApiPi is a drop-in OpenAI Agents API. Point official clients at this
gateway and bring your own model URL.

You create an agent, open a session, send messages, and stream events.
A session is a conversation that can use function tools, MCP servers,
and an optional computer. The computer can be a local session directory
or a runner you attach. Pi runs the agent loop behind the HTTP API. You
bring any OpenAI-compatible model endpoint. The package and CLI are
`apipi`. A hosted deploy lives at [geki.ai](https://geki.ai).

## What this is

Official OpenAI clients work for the subset we implement. Unknown
fields and unimplemented features return an error (`invalid_request` or
`not_implemented`). They are not stored and they are not ignored.

`environment.type` `openai_hosted` is OpenAI's field name for a **local
session directory** next to Pi. It is not OpenAI's cloud VM.

ApiPi is not a workflow builder, a model host, or a search engine. It
is not a copy of every OpenAI Agents object. There is no first-party
chat UI, ChatKit, or `/v1/chat/completions`. Search and browser attach
as MCP, not as built-in tools.

| You get | You bring |
| --- | --- |
| Agents, sessions, events, artifacts | An OpenAI-compatible model URL |
| Function tools, MCP, skills | A bearer the gateway does not store |
| A local directory or a `self_hosted` runner | Pi on `PATH` for live turns |

## Start

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
tools must set `host`. Full setup is in [Install](install.md).

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
OpenAI's Agents API quickstart are in [Quickstart](quickstart.md). The
HTTP surface is in [API](api.md).
