# ApiPi

ApiPi is an HTTP API that runs agent sessions. You create an agent, open a
session, send messages, and stream events. Pi is the loop behind the API.

Point it at any OpenAI-compatible model URL.

## Status

Gateway: `apipi serve`. Run mode `host`. `jail` and `microvm` exit.

## Read next

1. [Architecture](architecture.md)
2. [API](api.md)
3. [Environments](environments.md)
4. [Tools](tools.md)
5. [Decisions](decisions/index.md)

If you are writing code, also read [process](process.md) and
[agents](agents.md).

Not in this version: [roadmap](roadmap.md).

## Run

`apipi serve` starts the API.

| Config | Default | What |
| --- | --- | --- |
| `APIPI_RUN_MODE` | `jail` | `host` \| `jail` \| `microvm` |
| `APIPI_IDLE_TTL` | `15m` | Kill idle Pi/guest; session stays |
| `APIPI_EXAMPLE_UI` | off | Example chat at `/_example/` |

`host` is not suited for production. The process logs a warning.

## Stack

- Python 3.13, FastAPI, Postgres
- Pi over RPC, one process per session
- Run mode default: `jail`
- Default computer: session directory next to Pi
- MCP, function tools, and skills (`SKILL.md`)

Hosted: [geki.ai](https://geki.ai)
