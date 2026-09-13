# ApiPi

ApiPi is an open-source agent platform. [Pi](https://pi.dev) runs the
agent loop. You host the gateway, keep control of the agents, and point
them at your own LLM endpoints. Clients use an HTTP API compatible with
the [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api).

Production sessions run in [Firecracker](https://firecracker-microvm.github.io/)
microVMs. Each guest has its own kernel. The gateway stays on the host.
Pi, stdio MCP, and a local session directory share the guest.

[GEKI](https://geki.ai) also runs a managed ApiPi on European
infrastructure.

Official OpenAI clients work for the subset we implement. Unknown
fields return an error. `environment.type` `openai_hosted` is a local
session directory next to Pi (OpenAI's field name, a folder on your
machine).

| You get | You bring |
| --- | --- |
| Agents, sessions, events, artifacts | An OpenAI-compatible model URL |
| Function tools, MCP, skills | A bearer the gateway does not store |
| A local directory or a `self_hosted` runner | Pi on `PATH` for live turns |

## Quickstart

Python 3.13 and [uv](https://docs.astral.sh/uv/). From a checkout:

```
uv sync
npm i -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
export OPENAI_BASE_URL=http://your-model-host/v1
uv run apipi migrate
uv run apipi serve
```

`uv sync` does not put `apipi` on `PATH`; use `uv run`. `OPENAI_BASE_URL`
is the model host, not this API. The client bearer is the model key
unless you set `OPENAI_API_KEY_OVERWRITE`. That binds `0.0.0.0:8000`.
Default isolation is `none`. Production uses `APIPI_RUN_MODE=microvm`.
Details are on [Install](install.md).

Point a client at `http://localhost:8000/v1` with
`Authorization: Bearer`. Any non-empty bearer becomes a tenant.

The `openai` package is not an ApiPi dependency. In a second shell, point
the client at this gateway. `agent.model` must exist on the model host.

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

A full script is `examples/openai_sdk.py`. The same flow as OpenAI's
Agents API quickstart is on [Using the API](using.md).
