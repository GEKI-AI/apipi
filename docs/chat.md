# Chat fleets

Chat is a light Pi pool and a GEKI-native HTTP facade. Computer
agents stay on microVM workers. Both use the same session and event
store. Clients of `/v1/chat` never set or see `environment`.

This page is the operator layout and the product rules. Route fields
are in [API](api.md#chat). Worker pick rules are in
[sandbox workers](workers.md#placement).

## Fleet layout

Run one API-only gateway and two worker pools. Do not put chat and
computer sessions on the same `APIPI_RUN_MODE`.

```
apipi serve --api-only
APIPI_RUN_MODE=chat APIPI_WORKER_TOKEN=secret APIPI_API_URL=http://api.example:8000 apipi worker
APIPI_RUN_MODE=microvm APIPI_WORKER_TOKEN=secret APIPI_API_URL=http://api.example:8000 apipi worker
```

| Process | `APIPI_RUN_MODE` | What it serves |
| --- | --- | --- |
| `apipi serve --api-only` | unused for Pi | HTTP, store, placement |
| Chat worker | `chat` | Light Pi on the host. No Firecracker. Teardown kills the Pi process group. |
| Computer worker | `microvm` | One KVM guest per session |

`chat` is the same host backend as `none`, with a pool label so mixed
fleets can schedule. Keep `none` for laptops and CI. Combined
`apipi serve` still runs turns in-process and does not use worker
placement. After a chat-worker crash, the next start reaps leftover
host Pi processes from that worker. Use `KillMode=control-group` on
the systemd unit. `systemctl restart` sends SIGTERM so the worker
drains, then starts again. Install
`deploy/systemd/apipi-worker-drain.conf` so stop can wait for live Pi
to empty. Set `APIPI_PI_MEM_MIB` so one session cannot fill the worker.
Scrape `apipi_pi_processes` and `apipi_pi_rss_bytes` on the worker when
metrics are on. See
[sandbox workers](workers.md#drain-and-expiry) and
[observability](observability.md#prometheus).

## `/v1/chat`

`POST /v1/chat/sessions` creates a session in the same store as
Agents. The gateway stores `environment.type=none` and
`metadata.apipi.session_kind=chat`. Public chat JSON has no
`environment` field. Sending `environment` is `400` with code
`unknown_field`.

Do not document or send `environment.type=none` on the chat API.
That field is an Agents API value. Chat clients talk to `/v1/chat`
and omit environment.

Chat tools are function tools and HTTP MCP only. Stdio MCP, Playwright
auto-inject, workspace skills, and a computer are `400` with code
`chat_tool`. See [tools](tools.md).

To give a thread a computer later, create a **new** Agents session.
Chat does not upgrade in place in this version.

## Agents `environment.type=none`

The Agents API still accepts `environment.type=none` (no files, no
shell). That is not the chat product. Placement for those sessions
is `APIPI_ENV_NONE_PLACEMENT` / `[placement].env_none`:

| Value | What happens |
| --- | --- |
| `chat` (default) | Schedule onto `run_mode=chat` workers when that pool is present |
| `microvm` | Legacy: microVM workers. The guest still boots, which costs RAM for a session with no computer |
| `reject` | `400` with code `placement` |

`/v1/chat` always uses chat placement. The flag only affects raw
Agents + `environment.type=none`.

Footgun: a microVM-only fleet with the default `chat` placement
returns `429` `capacity` for Agents `type=none` until you add chat
workers or set `APIPI_ENV_NONE_PLACEMENT=microvm`. A mixed fleet
auto-routes those sessions onto chat workers so they do not occupy a
guest.

## Config

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_RUN_MODE` | `[sandbox].backend` | `none` | Worker process: `chat` for the chat pool, `microvm` for computers. `none` is local/CI. |
| `APIPI_ENV_NONE_PLACEMENT` | `[placement].env_none` | `chat` | Agents `environment.type=none` → `chat`, `microvm`, or `reject`. Ignored by `/v1/chat`. |
| `APIPI_API_ONLY` | `api_only` | off | API process with no in-process Pi. Turns lease a worker. |
| `APIPI_WORKER_TOKEN` | `worker_token` | unset | Shared secret for both pools. |

The full tables are in [configuration](config.md).

## Locked decisions

These are the Path A rules this version ships:

1. Agents `environment.type=none` auto-routes to the chat pool when
   present (default `APIPI_ENV_NONE_PLACEMENT=chat`).
2. Chat → computer is a new session only.
3. `/v1/chat` and placement live in core ApiPi.

A later optional non-Pi ChatHarness behind the same `/v1/chat` is
parked. This version ships light Pi.
