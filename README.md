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
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
apipi serve
```

That binds `0.0.0.0:8000`. The default isolation is `none` (Pi as a
child process). For Firecracker:

```
APIPI_RUN_MODE=microvm apipi serve
```

Point a client at `http://localhost:8000/v1` with
`Authorization: Bearer`. Any non-empty bearer becomes a tenant.

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

A full script is [examples/openai_sdk.py](examples/openai_sdk.py).
`OPENAI_BASE_URL` / `OPENAI_API_KEY` on the gateway process are the
**model** host, not this API.

## License

MIT. See [LICENSE](LICENSE). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
