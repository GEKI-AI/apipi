# Configuration

The gateway reads settings from environment variables, an optional
`.env` file, and an optional TOML file. Environment variables win.
`.env` wins over TOML. Defaults apply when nothing is set. Install the
process first ([Install](install.md)). Call it with
[Using the API](using.md).

This is the same model pydantic-settings uses in other Python services.
systemd `Environment=` / `EnvironmentFile=` and Kubernetes env work
without a file. A checkout can use `apipi.toml` plus `.env` for local
secrets.

## Load order

1. Environment variables (`APIPI_*`, plus `DATABASE_URL` and
   `OPENAI_*`).
2. `.env` in the process working directory, if that file exists.
3. A TOML file: `apipi serve --config PATH`, else `APIPI_CONFIG`, else
   `apipi.toml` in the working directory if it exists.
4. Defaults in code.

TOML keys are snake_case field names (`run_mode = "jail"`). Unknown
TOML keys fail at startup. A setting that would store prompt or
completion bodies is rejected at startup.

`--host` and `--port` on `apipi serve` override the bind from config.

Examples in this repo are `examples/apipi.toml` and
`examples/env.example`. Copy the env example to `.env` and fill in
secrets. Do not commit `.env`.

## Settings

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `DATABASE_URL` | `database_url` | required | Postgres URL. `postgresql+asyncpg://…` preferred. |
| `APIPI_RUN_MODE` | `run_mode` | `jail` | `host` \| `jail` \| `microvm`. Production SaaS/enterprise is `microvm`. `jail` is a fallback when KVM cannot run. `host` is local/dev. `jail` and `microvm` launch a throwaway sandbox before the API listens. If the mode cannot start, the process exits. No fallback. |
| `APIPI_HOST` | `host` | `0.0.0.0` | Bind address. |
| `APIPI_PORT` | `port` | `8000` | Bind port. |
| `APIPI_LOG_LEVEL` | `log_level` | `info` | `debug` \| `info` \| `warning` \| `error` \| `critical`. |
| `APIPI_IDLE_TTL` | `idle_ttl` | `15m` | Kill an idle Pi process to free RAM. The session row and `openai_hosted` directory stay. Resume from the event log. |
| `APIPI_WORKSPACE_TTL` | `workspace_ttl` | `1h` | Delete an `openai_hosted` directory after this long with no session activity, and only if Pi is already gone. Transcript and published artifacts stay. |
| `APIPI_MAX_SESSIONS` | `max_sessions` | `32` | Live Pi processes on this node. A new turn that would pass the cap returns `429` with code `capacity`. Idle reap frees a slot. Postgres session rows are not counted. |
| `APIPI_MAX_SESSIONS_PER_TENANT` | `max_sessions_per_tenant` | `32` | Live Pi processes for one tenant. A new turn that would pass the cap returns `429` with code `capacity_tenant`. The node cap still applies. |
| `APIPI_TURN_TIMEOUT` | `turn_timeout` | `10m` | Cancel a stuck turn. |
| `APIPI_AUTH` | `auth` | unset (default hash) | Import path `package.mod:func` for the auth callback. |
| `APIPI_AUTH_CACHE_TTL` | `auth_cache_ttl` | `30s` | Cache the callback result by SHA-256 of the bearer, never the raw key. |
| `APIPI_PI_COMMAND` | `pi_command` | `pi` | Pi binary used as `pi --mode rpc`. |
| `APIPI_SESSIONS_DIR` | `sessions_dir` | `.apipi/sessions` under cwd | Root for local session directories (`openai_hosted`). |
| `APIPI_JAIL_MEMORY` | `jail_memory` | `512M` | cgroup `memory.max` for each jailed Pi. |
| `APIPI_MICROVM_KERNEL` | `microvm_kernel` | unset | Guest kernel image. Required when `run_mode` is `microvm`. |
| `APIPI_MICROVM_ROOTFS` | `microvm_rootfs` | unset | Guest rootfs image. Required when `run_mode` is `microvm`. Do not vendor a distro in git. |
| `APIPI_MICROVM_MEM_MIB` | `microvm_mem_mib` | `512` | Guest RAM in MiB. |
| `APIPI_MICROVM_VCPUS` | `microvm_vcpus` | `1` | Guest vCPUs. |
| `APIPI_MICROVM_EGRESS_ALLOWLIST` | `microvm_egress_allowlist` | on | When `run_mode` is `microvm`, guest TAP egress may reach only the model host, this session's HTTP MCP hosts, `microvm_egress_hosts`, and DNS. Unlisted TCP is rejected. Off keeps open TAP egress (lab). |
| `APIPI_MICROVM_EGRESS_HOSTS` | `microvm_egress_hosts` | empty | Extra hostnames the guest may reach, comma-separated. |
| `APIPI_MICROVM_EGRESS_MBIT` | `microvm_egress_mbit` | `50` | `tc` rate on each guest TAP, both directions. |
| `APIPI_DB_POOL_SIZE` | `db_pool_size` | `5` | SQLAlchemy pool size. |
| `APIPI_MAX_REQUEST_BYTES` | `max_request_bytes` | `1MiB` | Reject larger request bodies with `413` and code `payload_too_large`. |
| `APIPI_MAX_WORKSPACE_BYTES` | `max_workspace_bytes` | `1GiB` | Size of one `openai_hosted` session directory. An oversized microvm pull is not unpacked. Over the cap, harvest emits `agent.session.error` with code `workspace_too_large`. |
| `APIPI_MAX_ARTIFACT_BYTES` | `max_artifact_bytes` | `512MiB` | Host artifact store per session. Publishing more is refused with code `artifact_too_large`. |
| `OPENAI_BASE_URL` | `model_base_url` | unset | Model host passed to Pi. Not the gateway URL. |
| `OPENAI_API_KEY` | `model_api_key` | unset | Model key passed to Pi. |
| `APIPI_METRICS` | `metrics` | off | Prometheus text at `/metrics` when on. No bearer. |
| `APIPI_OTEL_ENDPOINT` | `otel_endpoint` | unset | OTLP/HTTP traces when set. `/v1/traces` is appended if missing. |
| `APIPI_CONFIG` | — | unset | Path to a TOML file. Ignored when `apipi serve --config` is set. |

Durations are like `15m`, `30s`, `2h`. Sizes are like `512M` or `1MiB`
(1024-based).

## Run mode

Run mode is server config, not an OpenAI field. The process default is
`jail` so a machine without KVM can still start. Production operators
set `microvm`. Operators without jail tools must set `host`. `host`
logs a warning and is not suited for production. What to install,
systemd, and when to use each mode are in [run modes](run-modes.md).

```toml
run_mode = "microvm"
```

```
APIPI_RUN_MODE=microvm apipi serve
```

## Auth callback

Unset `auth` uses the default hash in this package. Set an import path
when you already have a bearer from an LLM router:

```toml
auth = "mycompany.apipi_auth:authenticate"
auth_cache_ttl = "30s"
```

```
export APIPI_AUTH=mycompany.apipi_auth:authenticate
```

The function is `authenticate(bearer) -> {key_id, tenant_id} | reject`.
See [auth](auth.md) and `examples/auth_callback.py`.

## Limits

These are operator settings, not customer tiers. There is no plan or
SKU field. One `apipi serve` process has one profile. Change a setting
and restart the process. Do not add uvicorn workers; the pool is in
memory in that process.

Each live session is one Pi process (or guest). Without a cap, a burst
of sessions can exhaust RAM and PIDs. `max_sessions` counts those live
processes on the node. `max_sessions_per_tenant` counts them for one
tenant. The session row in Postgres can outlive the process; idle TTL
kills the process and frees a slot.

`turn_timeout` stops a generate that never returns. `workspace_ttl`
deletes the local computer directory after idle Pi has already been
killed. `jail_memory` and the microvm memory/vCPU settings bound each
worker. `max_request_bytes` bounds HTTP bodies. `max_workspace_bytes`
bounds one `openai_hosted` directory. `max_artifact_bytes` bounds the
published host store for one session. `db_pool_size` bounds connections
to Postgres.

Code defaults are conservative. Production SaaS and enterprise should
set `run_mode` to `microvm` and size the rest to the host. Raise
`microvm_mem_mib` when you enable heavy stdio MCP such as Playwright.
Do not add browser tiers. On a shared node, set
`max_sessions_per_tenant` lower than `max_sessions` (for example `8`).
Size `max_sessions` to host RAM divided by `microvm_mem_mib`. Keep
`max_workspace_bytes` at `1GiB` and `max_artifact_bytes` at `512MiB`
unless the computer must hold more. Microvm TAP egress is allowlisted
and capped at `50` Mbit by default. Add extra hosts with
`microvm_egress_hosts`. The gateway's own HTTP MCP probe stays on the
host; Pi still dials those URLs from the guest, so those hosts are
added to the TAP allowlist when the session starts. Jail/pasta egress
is not filtered by these settings. Change a setting and restart.

| Failure | HTTP or event | Code |
| --- | --- | --- |
| Node live-session cap | `429` | `capacity` |
| Per-tenant live-session cap | `429` | `capacity_tenant` |
| Request body too large | `413` | `payload_too_large` |
| Workspace directory too large | `agent.session.error` | `workspace_too_large` |
| Artifact store too large | `agent.session.error` | `artifact_too_large` |

The gateway does not intercept every write inside a jail or guest.
Guest tmpfs is already bounded by `microvm_mem_mib`. Workspace and
artifact caps are enforced when the host unpacks or publishes. Host
and jail files that are already on disk stay until workspace TTL.
`self_hosted` runner disk is not capped; bytes published onto the
gateway still count toward `max_artifact_bytes`.

## TOML example

```toml
database_url = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
run_mode = "host"
host = "0.0.0.0"
port = 8000
log_level = "info"
idle_ttl = "15m"
workspace_ttl = "1h"
max_sessions = 32
max_sessions_per_tenant = 32
turn_timeout = "10m"
auth_cache_ttl = "30s"
jail_memory = "512M"
microvm_egress_allowlist = true
microvm_egress_hosts = ""
microvm_egress_mbit = 50
max_request_bytes = "1MiB"
max_workspace_bytes = "1GiB"
max_artifact_bytes = "512MiB"
db_pool_size = 5
metrics = false
```

Put `OPENAI_API_KEY` in `.env` or the process environment, not in a
committed TOML file.
