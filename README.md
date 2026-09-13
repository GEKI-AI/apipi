# ApiPi

Documentation: [geki-ai.github.io/apipi](https://geki-ai.github.io/apipi/)

ApiPi is an open-source agent platform. [Pi](https://pi.dev) runs the
agent loop. You host the gateway, keep control of the agents, and point
them at your own LLM endpoints. Clients use an HTTP API compatible with
the [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api).

Production sessions run in [Firecracker](https://firecracker-microvm.github.io/)
microVMs: each guest has its own kernel. The gateway stays on the host.
Pi and the session files share the guest.

[GEKI](https://geki.ai) also runs a managed ApiPi on European
infrastructure.

## Quickstart

Python 3.13 and [uv](https://docs.astral.sh/uv/). Postgres for the
store. The Pi CLI (`pi --mode rpc`) on `PATH` for live turns.

```
uv sync
npm i -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
export OPENAI_BASE_URL=http://your-model-host/v1
uv run apipi migrate
uv run apipi serve
```

`uv sync` does not put `apipi` on `PATH`; use `uv run`. That binds
`0.0.0.0:8000`. The default isolation is `none` (Pi as a child
process). `OPENAI_BASE_URL` on the gateway is the **model** host, not
this API. The client bearer is the model key unless you set
`OPENAI_API_KEY_OVERWRITE`. For Firecracker:

```
APIPI_RUN_MODE=microvm uv run apipi serve
```

In a second shell, point a client at `http://localhost:8000/v1`.
`agent.model` must exist on the model host. The `openai` package is not
an ApiPi dependency:

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

A full script is [examples/openai_sdk.py](examples/openai_sdk.py).
The stream stays open across idle, so that script stops after the first
turn outcome.

## License

MIT. See [LICENSE](LICENSE). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
