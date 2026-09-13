# Examples

MCP configs, gateway config samples, a small OpenAI Python SDK
script, a self_hosted runner, and a local chat playground. Keys come
from the environment, not from these files.

| File | What |
| --- | --- |
| [openai_sdk.py](openai_sdk.py) | Official OpenAI Python client against this API |
| [self_hosted_runner.py](self_hosted_runner.py) | Local directory as a `self_hosted` computer |
| [playground/](playground/) | Vite React playground (sessions, turns, artifacts) |
| [apipi.toml](apipi.toml) | Gateway settings file |
| [env.example](env.example) | Dotenv template; copy to `.env` |
| [auth_callback.py](auth_callback.py) | Auth callback (`APIPI_AUTH`) |
| [tavily.yaml](tavily.yaml) | Web search (Tavily hosted MCP) |
| [playwright.yaml](playwright.yaml) | Browser (Playwright MCP, headless) |

Skills are `SKILL.md` directories on the computer. Point at them with
`environment.capability_directories`. See [docs/tools.md](../docs/tools.md).

## OpenAI Python SDK

[openai_sdk.py](openai_sdk.py) uses the official `OpenAI` client with
`base_url` pointed at this gateway. It creates a session with an inline
agent, `environment={"type": "openai_hosted"}` (a local session
directory, not OpenAI's cloud), and streams the first turn. The input
asks the agent to write `tree.py`, run it, and show the output. The
script prints SSE `data:` lines and stops after the first turn outcome.
The stream stays open across idle, so a loop without that stop would
wait on keepalives.

It only sends fields this API implements, so the SDK does not add
`multi_agent`, `vault_ids`, or other unknown keys that would return
`400`.

The `openai` package is not an ApiPi dependency. Install it for this
script only:

```
uv run --with openai python examples/openai_sdk.py
```

The gateway must already be running. Jail is the default when `bwrap`,
`pasta`, and cgroup v2 are present. Operators without those tools
should serve with host mode. `microvm` starts when `/dev/kvm`,
Firecracker, jailer, guest images, `ip`, and `iptables` are present;
otherwise serve exits. Send a bearer the client will send:

```
APIPI_RUN_MODE=host apipi serve
```

Default auth accepts any non-empty bearer and hashes it into a tenant
id. Set the same value on the client. For this script,
`OPENAI_BASE_URL` is the ApiPi gateway, not the model host that Pi
uses. If you omit it, the default is `http://localhost:8000/v1`.

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

The product [Using the API](../docs/using.md) page walks through the same
client: run a task, follow progress, continue, and delete.

## self_hosted runner

[self_hosted_runner.py](self_hosted_runner.py) attaches a local
directory as the session computer. Create a session with
`environment.type` `self_hosted`. The create response includes
`environment_id` and a one-time `key`. The runner opens
`/v1/environments/{environment_id}` as a WebSocket and sends `hello`
with that key. After that it serves `exec`, `read`, `write`, `edit`,
`list`, `artifact`, `ping`, and `close` against the directory. The
protocol is in [docs/environments.md](../docs/environments.md).

The `websockets` package is not an ApiPi dependency. Install it for
this script only. The gateway must already be running. `OPENAI_BASE_URL`
is the ApiPi gateway, the same meaning as in [openai_sdk.py](openai_sdk.py).
If you omit it, the default is `http://localhost:8000/v1`. Put the
one-time key in the environment, not in the file:

```
export OPENAI_BASE_URL=http://localhost:8000/v1
export APIPI_ENVIRONMENT_ID=...
export APIPI_ENVIRONMENT_KEY=...
uv run --with websockets python examples/self_hosted_runner.py --dir ./workspace
```

`--dir` is the workspace. You can also set `APIPI_RUNNER_DIR`. File
and shell tools reach that folder over the socket once the runner is
connected.

## Playground

[playground/](playground/) is a local React app that talks to this API
through a Vite proxy. It lists sessions, creates a session with a saved
agent or an inline agent, streams turns, shows tool and command
activity, deletes a session, and downloads artifacts. It is an example
client, not a first-party UI. GitHub CI does not run it.

The proxy injects `Authorization: Bearer` from `OPENAI_API_KEY`. That
value is the gateway bearer (a local token, or a Geki tenant key), not
the model host key that Pi uses. The browser never sees it. Default
auth accepts any non-empty bearer. `OPENAI_BASE_URL` is the ApiPi
gateway, the same meaning as in [openai_sdk.py](openai_sdk.py).

The gateway must already be running. From `examples/playground`:

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
npm install
npm run dev
```

That binds `0.0.0.0:8100` and proxies `/v1` to the gateway. Open
http://localhost:8100 . If `OPENAI_API_KEY` is unset, the proxy sends
`dev-token`.

Artifact bytes exist after Pi stops and copies `artifacts/` into the
host store. An empty list during a live turn is expected. Idle TTL is
`APIPI_IDLE_TTL` (default 15 minutes).
