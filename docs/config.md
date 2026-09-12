# Configuration

The gateway reads settings from environment variables, an optional
`.env` file, and an optional TOML file. Environment variables win.
`.env` wins over TOML. Defaults apply when nothing is set.

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
| `APIPI_RUN_MODE` | `run_mode` | `jail` | `host` \| `jail` \| `microvm`. If the mode cannot start, the process exits. No fallback. |
| `APIPI_HOST` | `host` | `0.0.0.0` | Bind address. |
| `APIPI_PORT` | `port` | `8000` | Bind port. |
| `APIPI_LOG_LEVEL` | `log_level` | `info` | `debug` \| `info` \| `warning` \| `error` \| `critical`. |
| `APIPI_IDLE_TTL` | `idle_ttl` | `15m` | Kill an idle Pi process. The session row stays. Resume from the event log. |
| `APIPI_MAX_SESSIONS` | `max_sessions` | `32` | Live Pi processes. A new turn that would pass the cap returns `429` with code `capacity`. Idle reap frees a slot. Postgres session rows are not counted. |
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
| `APIPI_DB_POOL_SIZE` | `db_pool_size` | `5` | SQLAlchemy pool size. |
| `APIPI_MAX_REQUEST_BYTES` | `max_request_bytes` | `1MiB` | Reject larger request bodies with `413` and code `payload_too_large`. |
| `OPENAI_BASE_URL` | `model_base_url` | unset | Model host passed to Pi. Not the gateway URL. |
| `OPENAI_API_KEY` | `model_api_key` | unset | Model key passed to Pi. |
| `APIPI_METRICS` | `metrics` | off | Prometheus text at `/metrics` when on. No bearer. |
| `APIPI_OTEL_ENDPOINT` | `otel_endpoint` | unset | OTLP/HTTP traces when set. `/v1/traces` is appended if missing. |
| `APIPI_CONFIG` | — | unset | Path to a TOML file. Ignored when `apipi serve --config` is set. |

Durations are like `15m`, `30s`, `2h`. Sizes are like `512M` or `1MiB`
(1024-based).

## Run mode

Run mode is server config, not an OpenAI field. `jail` is the default.
Operators without jail tools must set `host`. `host` logs a warning and
is not suited for production. What to install, systemd, and when to
use each mode are in [run modes](run-modes.md).

```toml
run_mode = "jail"
```

```
APIPI_RUN_MODE=host apipi serve
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

Each live session is one Pi process (or guest). Without a cap, a burst
of sessions can exhaust RAM and PIDs. `max_sessions` counts those live
processes. The session row in Postgres can outlive the process; idle
TTL kills the process and frees a slot.

`turn_timeout` stops a generate that never returns. `jail_memory` and
the microvm memory/vCPU settings bound each worker. `max_request_bytes`
bounds HTTP bodies. `db_pool_size` bounds connections to Postgres.

One `apipi serve` is one process. Do not add uvicorn workers; the pool
is in memory in that process.

## TOML example

```toml
database_url = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
run_mode = "host"
host = "0.0.0.0"
port = 8000
log_level = "info"
idle_ttl = "15m"
max_sessions = 32
turn_timeout = "10m"
auth_cache_ttl = "30s"
jail_memory = "512M"
max_request_bytes = "1MiB"
db_pool_size = 5
metrics = false
```

Put `OPENAI_API_KEY` in `.env` or the process environment, not in a
committed TOML file.
