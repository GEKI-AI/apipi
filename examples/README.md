# Examples

Copy-paste configs, operator samples, session clients, and a local
chat playground. Keys come from the environment, not from these files.

| Path | What |
| --- | --- |
| [sessions/](sessions/) | Python scripts that create a session and stream a turn |
| [extend_fastapi.py](extend_fastapi.py) | Extend ApiPi: Agents API plus `GET /ok` in one FastAPI app. Run `apipi migrate` on that SQLite file first. See [Extending ApiPi](../docs/extending.md). |
| [playground/](playground/) | Vite React playground (agents, sessions, turns, artifacts) |
| [self_hosted_runner.py](self_hosted_runner.py) | Local directory as a `self_hosted` computer |
| [apipi.toml](apipi.toml) | Gateway settings file |
| [env.example](env.example) | Dotenv template; copy to `.env` at the repo root |
| [auth_callback.py](auth_callback.py) | Auth callback (`APIPI_AUTH`) |
| [isolation.py](isolation.py) | Custom isolation backend (`APIPI_RUN_MODE`) |
| [tavily.yaml](tavily.yaml) | Web search (Tavily hosted MCP) |
| [playwright.yaml](playwright.yaml) | Browser (Playwright MCP, headless) |

Session clients live in [sessions/](sessions/). How to start a split
`microvm` API plus worker and run every session script is in
[sessions/README.md](sessions/README.md) and
[Manual microvm examples](../docs/tests.md#manual-microvm-examples).
GitHub CI does not run that suite.

Skills are `SKILL.md` directories on the computer. Point at them with
`environment.capability_directories`. See [docs/tools.md](../docs/tools.md).

## Playground

[playground/](playground/) is a local React app that talks to this API
through a Vite proxy. It lists models, creates a saved agent with a
model and instructions, lists sessions, creates a session with a saved
agent, streams turns, shows tool and command activity, deletes a
session, and downloads artifacts. It is an example client, not a
first-party UI. GitHub CI does not run it. It is not part of
`sessions/run-microvm.sh`.

The proxy injects `Authorization: Bearer` from `API_KEY`. That value is
the gateway bearer (a local token, or a Geki tenant key), not the model
host key that Pi uses. The browser never sees it. Default auth accepts
any non-empty bearer. `API_BASE_URL` is the ApiPi gateway. Those names
are not `OPENAI_API_KEY` / `OPENAI_BASE_URL`, which on the gateway
process mean the model host.

The gateway must already be running. From `examples/playground`, copy
`.env.example` to `.env` or export the same variables:

```
export API_KEY=dev-token
export API_BASE_URL=http://localhost:8000/v1
npm install
npm run dev
```

Vite loads `.env` when the playground server starts. That binds
`0.0.0.0:8100` and proxies `/v1` to the gateway. Open
http://localhost:8100 . If `API_KEY` is unset, the proxy sends
`dev-token`. Create an agent from the model list before you start a
session. `GET /v1/models` on the gateway must be enabled
(`APIPI_FORWARD_MODELS`, on by default).

Artifact bytes exist after a turn completes. Files under `outputs/`
are copied into the host store then. Idle Pi TTL for `none` and
`self_hosted` is `APIPI_IDLE_TTL` (default 15 minutes). A hosted
workspace lasts until `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1
hour).

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
is the ApiPi gateway. If you omit it, the default is
`http://localhost:8000/v1`. Put the one-time key in the environment,
not in the file:

```
export OPENAI_BASE_URL=http://localhost:8000/v1
export APIPI_ENVIRONMENT_ID=...
export APIPI_ENVIRONMENT_KEY=...
uv run --with websockets python examples/self_hosted_runner.py --dir ./workspace
```

`--dir` is the workspace. You can also set `APIPI_RUNNER_DIR`. File
and shell tools reach that folder over the socket once the runner is
connected.
