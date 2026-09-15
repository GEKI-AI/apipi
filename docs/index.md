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

Unknown JSON keys return `invalid_request`. Known OpenAI fields we
have not implemented return `not_implemented`. See
[OpenAI compatibility](openai-compatibility.md) for the matrix.
`environment.type` `openai_hosted` is a local session directory next to
Pi (OpenAI's field name for a folder on your machine).

| You get | You bring |
| --- | --- |
| Agents, sessions, events, artifacts | An OpenAI-compatible model URL |
| Function tools, MCP, skills | A bearer the gateway maps to a tenant |
| A local directory or a `self_hosted` runner | Pi on `PATH` for live turns |

## Quickstart

You need Python 3.13 and a model host URL. Live turns also need the Pi
CLI (`pi --mode rpc`) on `PATH`. The gateway pins Pi 0.85.1; `apipi
install` puts that binary in a user-local prefix.

A single process stores data in SQLite at `.apipi/apipi.db` and binds
`0.0.0.0:8000`. `OPENAI_BASE_URL` on the gateway is the model host that
Pi calls. Clients send `Authorization: Bearer`; any non-empty bearer
becomes a tenant, and that value is the model key unless you set
`OPENAI_API_KEY_OVERWRITE`. Isolation defaults to `none`. For
production, set `APIPI_RUN_MODE=microvm`. Several processes share
Postgres. Details are on [Install](install.md).

### From PyPI

```
pip install geki-apipi
apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
apipi migrate
apipi serve
```

`uv add geki-apipi` works in a project. The import package and CLI are
`apipi`. S3-compatible artifact storage is `pip install "geki-apipi[s3]"`.

### From a git checkout

```
git clone https://github.com/GEKI-AI/apipi.git
cd apipi
uv sync
uv run apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
uv run apipi migrate
uv run apipi serve
```

Prefix every `apipi` command with `uv run` in a checkout.

### Client

In a second shell, point a client at `http://localhost:8000/v1`.
`agent.model` must exist on the model host. Install the `openai`
package yourself if you want the official client:

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

A full script is `examples/openai_sdk.py`. The same flow as OpenAI's
Agents API quickstart is on [Using the API](using.md).
