# Configuration

Operator settings fall into three areas: the **gateway** (HTTP process,
Postgres, auth, limits, usage), the **Pi harness** (how ApiPi launches
Pi), and the **sandbox** (isolation backend, guest images, resources,
and networking). Put the bulk of that in a TOML file. Keep secrets and
sparse overrides in the process environment or `.env`. What those
areas mean is in [Concepts](concepts.md):
[isolation](isolation.md) and [workers](worker-concepts.md).

The example file in this repo is `examples/apipi.toml`. Copy
`examples/env.example` to `.env` for secrets.

## Load order

1. Environment variables (`APIPI_*`, plus `DATABASE_URL` and
   `OPENAI_*`).
2. `.env` in the process working directory, if that file exists.
3. A TOML file: `apipi serve --config PATH`, else `APIPI_CONFIG`, else
   `apipi.toml` in the working directory if it exists.
4. Defaults in code.

Environment variables win. `.env` wins over TOML. Unknown TOML keys
fail at startup. Nested tables are `[pi]`, `[sandbox]`,
`[sandbox.resources]`, `[sandbox.network]`, `[sandbox.ttl]`, and
`[placement]`. A setting that would
store prompt or completion bodies is rejected at startup.

`load_settings()` is the CLI path and still reads the process
environment and `.env`. `extend_settings(...)` builds `Settings` from
the arguments only. It does not read `DATABASE_URL`, `OPENAI_*`, or
other host environment values. Use it when another app in the same
process already owns those names.

`--host` and `--port` on `apipi serve` override the bind from config.

Durations are like `15m`, `30s`, `2h`, `15d`. Sizes are like `512M` or
`1MiB` (1024-based).

## Gateway

The gateway is the HTTP product: bind address, store, auth, live
session caps, artifact store, usage export, and TTLs. Guest RAM and Pi
CLI flags live under sandbox and harness.

Unset `auth` uses the default hash in this package. Set an import path
when you already have a bearer from an LLM router. See [auth](auth.md)
and `examples/auth_callback.py`.

Extension points use the same import-path idea: `APIPI_AUTH` for the
callback, `[sandbox].backend = "package.mod:Class"` for a custom
isolation backend, `usage_sinks` / `payload_sinks` for extra export
handlers, and `artifact_store` for local or S3 object bytes (artifacts, and
hosted files and skills).

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `DATABASE_URL` | `database_url` | `.apipi/apipi.db` (SQLite) | Store URL. Unset uses SQLite in the current directory. File SQLite uses WAL. One process only. Shared store: `postgresql+asyncpg://…`. |
| `APIPI_HOST` | `host` | `0.0.0.0` | Bind address. |
| `APIPI_PORT` | `port` | `8000` | Bind port. |
| `APIPI_INSTANCE_ID` | `instance_id` | unset | Short name for this process. When set, HTTP responses except `/health` include `X-ApiPi-Instance`. Used to confirm stickiness on [multiple nodes](scale.md). |
| `APIPI_LOG_LEVEL` | `log_level` | `info` | `debug` \| `info` \| `warning` \| `error` \| `critical`. |
| `APIPI_LOG_FORMAT` | `log_format` | `json` | `json` (one object per line on stderr) or `text` (laptop). |
| `APIPI_IDLE_TTL` | `idle_ttl` | `15m` | Kill an idle Pi process for `none` and `self_hosted` sessions to free RAM. Hosted computers use the sandbox TTL instead. |
| `APIPI_SANDBOX_TTL_OPENAI_HOSTED` | `[sandbox.ttl].openai_hosted` | `1h` | Stop Pi and delete the `openai_hosted` workspace after this idle. Transcript and published artifacts stay. `0` turns the timer off. `APIPI_WORKSPACE_TTL` / `workspace_ttl` is an alias. |
| `APIPI_SANDBOX_TTL_SELF_HOSTED` | `[sandbox.ttl].self_hosted` | `0` (off) | Idle policy for `self_hosted`. The gateway cannot delete files on the runner. `0` means off. |
| `APIPI_MAX_SESSIONS` | `max_sessions` | `32` | Live Pi processes on this node. A new turn that would pass the cap returns `429` with code `capacity`. Idle reap frees a slot. Postgres session rows are not counted. Workers advertise this as `capacity`. |
| `APIPI_MAX_SESSIONS_PER_TENANT` | `max_sessions_per_tenant` | `32` | Live Pi processes for one tenant. A new turn that would pass the cap returns `429` with code `capacity_tenant`. The node cap still applies. |
| `APIPI_WORKER_MEMORY_MB` | `worker_memory_mb` | `max_sessions × mem_mib` (16384 at defaults) | RAM budget this worker (or combined node) will run, in MiB. Sum of guest `mem_mib` for live leases must stay under this. Set it to usable host RAM minus OS and worker reserve. Do not read `/proc/meminfo` automatically. |
| `APIPI_TURN_TIMEOUT` | `turn_timeout` | `10m` | Cancel a stuck turn. |
| `APIPI_AUTH` | `auth` | unset (default hash) | Import path `package.mod:func` for the auth callback. The callback may return a typed reject (`401` or `429`). |
| `APIPI_WORKER_TOKEN` | `worker_token` | unset | Shared secret for `apipi worker` connections. Compared in memory. Not a tenant key and not stored in the database. Unset rejects the worker socket. Put this in the process environment. See [workers](workers.md). |
| `APIPI_WORKER_LEASE_TTL` | `worker_lease_ttl` | `30s` | How long a session lease stays valid without a heartbeat. Expiry fails closed and emits `agent.session.error` with code `worker_lease_expired`. |
| `APIPI_API_URL` | `api_url` | unset (`http://127.0.0.1:8000` for `apipi worker`) | Base URL the worker uses to open `/internal/worker`. |
| `APIPI_API_ONLY` | `api_only` | off | Control plane only. Turns lease a worker. `apipi serve --api-only` sets this. |
| `APIPI_ENV_NONE_PLACEMENT` | `[placement].env_none` | `chat` | Where Agents sessions with `environment.type=none` run on a mixed fleet: `chat` (chat workers), `microvm` (legacy computer workers), or `reject` (`400` code `placement`). Session metadata `apipi.session_kind=chat` always uses chat workers. Computer environments always use `microvm`. See [chat fleets](chat.md) and [workers](workers.md). |
| `APIPI_AUTH_CACHE_TTL` | `auth_cache_ttl` | `30s` | Cache success and `401` rejects by SHA-256 of the bearer, never the raw key. `429` rejects are not cached. |
| `APIPI_SESSIONS_DIR` | `sessions_dir` | `.apipi/sessions` under cwd | Root for local session directories (`openai_hosted`). |
| `APIPI_DB_POOL_SIZE` | `db_pool_size` | `5` | SQLAlchemy pool size. |
| `APIPI_MAX_REQUEST_BYTES` | `max_request_bytes` | `1MiB` | Reject larger request bodies with `413` and code `payload_too_large`. |
| `APIPI_MAX_WORKSPACE_BYTES` | `max_workspace_bytes` | `1GiB` | Size of one `openai_hosted` session directory. An oversized microvm pull is not unpacked. Over the cap, harvest emits `agent.session.error` with code `workspace_too_large`. |
| `APIPI_MAX_ARTIFACT_BYTES` | `max_artifact_bytes` | `512MiB` | Published artifact bytes per session. Publishing more is refused with code `artifact_too_large`. The harness session cache uses the same blob store and does not count toward this cap. |
| `APIPI_MAX_FILE_BYTES` | `max_file_bytes` | `50MiB` | Max size of one `POST /v1/files` or `POST /v1/skills` upload. Larger bodies return `413` with code `payload_too_large`. JSON routes still use `max_request_bytes`. |
| `APIPI_ARTIFACT_STORE` | `artifact_store` | `local` | `local` or `s3`. Published artifacts, hosted file uploads, and hosted skill bundles share this backend. Local artifacts stay under `APIPI_SESSIONS_DIR/.artifacts`. Local files and skills stay under `APIPI_SESSIONS_DIR/.store/files` and `.store/skills`. |
| `APIPI_S3_BUCKET` | `s3_bucket` | required if s3 | Bucket. |
| `APIPI_S3_ENDPOINT` | `s3_endpoint` | unset | Base URL for S3-compatible APIs (Hetzner, MinIO, R2). Unset talks to AWS. |
| `APIPI_S3_REGION` | `s3_region` | `us-east-1` | Region (`hel1`, `fsn1`, `nbg1` on Hetzner). |
| `APIPI_S3_PREFIX` | `s3_prefix` | `apipi/artifacts` | Artifact key prefix. Artifact objects are `{prefix}/{tenant_id}/{key_id}/{session_id}/{artifact_id}`. When the prefix ends with `/artifacts` (the default), files and skills use sibling prefixes `…/files` and `…/skills`. Otherwise they are `{prefix}/files` and `{prefix}/skills`. |
| `APIPI_S3_ADDRESSING` | `s3_addressing` | `auto` | `auto` \| `path` \| `virtual`. `auto` uses path-style when `s3_endpoint` is set. |
| `OPENAI_BASE_URL` | `model_base_url` | required for serve | Model host passed to Pi. Not the gateway URL. Put this in `.env`. |
| `OPENAI_API_KEY_OVERWRITE` | `model_api_key_overwrite` | unset | Optional operator model key. When unset, Pi gets the request bearer. A process `OPENAI_API_KEY` is ignored. |
| `APIPI_FORWARD_MODELS` | `forward_models` | on | Proxy `GET /v1/models` to `{OPENAI_BASE_URL}/models`. Off returns `400` with code `forward_models`. |
| `APIPI_USAGE_STORE` | `usage_store` | `turns` | How much agent usage hits Postgres: `off` \| `rollups` \| `turns`. See [usage](usage.md). |
| `APIPI_USAGE_RETENTION` | `usage_retention` | `15d` | Delete turn log rows older than this. Empty means no purge. Rollups stay. |
| `APIPI_USAGE_EXPORT_URL` | `usage_export_url` | unset | HTTPS POST of one non-text agent usage event per turn. Off when unset. |
| `APIPI_USAGE_EXPORT_TOKEN` | `usage_export_token` | unset | Bearer for the usage export URL. Put this in the process environment. |
| `APIPI_USAGE_EXPORT_TIMEOUT` | `usage_export_timeout` | `5s` | Timeout for each export attempt. |
| `APIPI_USAGE_EXPORT_RETRIES` | `usage_export_retries` | `1` | Extra tries after the first, then drop. A failed export does not break the turn. |
| `APIPI_PAYLOAD_EXPORT_URL` | `payload_export_url` | unset | HTTPS POST of one agent payload (session text and tool args) per turn. Off when unset. |
| `APIPI_PAYLOAD_EXPORT_TOKEN` | `payload_export_token` | unset | Bearer for the payload export URL. Put this in the process environment. |
| `APIPI_PAYLOAD_EXPORT_TIMEOUT` | `payload_export_timeout` | `5s` | Timeout for each payload export attempt. |
| `APIPI_PAYLOAD_EXPORT_RETRIES` | `payload_export_retries` | `1` | Extra tries after the first, then drop. A failed export does not break the turn. |
| `APIPI_USAGE_SINKS` | `usage_sinks` | empty | Extra usage sinks, comma-separated `package.mod:Class`. |
| `APIPI_PAYLOAD_SINKS` | `payload_sinks` | empty | Extra payload sinks, comma-separated `package.mod:Class`. |
| `APIPI_METRICS` | `metrics` | off | Prometheus text at `/metrics` when on. No bearer. Combined `apipi serve` scrapes the API. `apipi worker` also binds `/metrics` on `APIPI_WORKER_METRICS_HOST`:`APIPI_WORKER_METRICS_PORT`. |
| `APIPI_WORKER_METRICS_HOST` | `worker_metrics_host` | `0.0.0.0` | Bind address for the worker scrape endpoint. |
| `APIPI_WORKER_METRICS_PORT` | `worker_metrics_port` | `9091` | Port for the worker scrape endpoint. |
| `APIPI_GUEST_SAMPLE_INTERVAL` | `guest_sample_interval` | unset | How often the worker pulls a tiny vsock snapshot (CPU/load, MemAvailable, workspace disk). Unset is off. Host cgroup CPU+RAM is on whenever worker metrics are on. |
| `APIPI_OTEL_ENDPOINT` | `otel_endpoint` | unset | OTLP/HTTP traces when set. `/v1/traces` is appended if missing. |
| `APIPI_CONFIG` | — | unset | Path to a TOML file. Ignored when `apipi serve --config` is set. |

How to collect those signals in production is in
[observability](observability.md).

```toml
database_url = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
host = "0.0.0.0"
port = 8000
log_level = "info"
log_format = "json"
idle_ttl = "15m"
max_sessions = 32
max_sessions_per_tenant = 32
worker_memory_mb = 16384
turn_timeout = "10m"
auth_cache_ttl = "30s"
max_request_bytes = "1MiB"
max_workspace_bytes = "1GiB"
max_artifact_bytes = "512MiB"
max_file_bytes = "50MiB"
db_pool_size = 5
usage_store = "turns"
usage_retention = "15d"
metrics = false
forward_models = true
```

```toml
auth = "mycompany.apipi_auth:authenticate"
auth_cache_ttl = "30s"
```

Logs are JSON lines on stderr. Collectors should scrape that stream.
Each line has `timestamp`, `level`, `logger`, `message`, and
`service` (`apipi`). Context fields (`request_id`, `tenant_id`,
`session_id`, `turn_id`, `instance_id`, `run_mode`) are present when
known. Error and warning lines that operators should alert on also set
`event` and `error_code`. Default level is `info`: process start, one
HTTP request line (not `/health` or `/metrics`), and turn completed or
cancelled. Failed turns and unexpected exceptions are `error`. `debug`
is optional. Prompt and completion bodies are never logged.
`APIPI_LOG_FORMAT=text` restores the old one-line format. The event
table is in [usage](usage.md#logs).

One `apipi serve` process has one profile. Change a setting and restart.
The Pi pool is in memory in that process, so extra uvicorn workers do
not share it. Several processes behind a load balancer need session
affinity ([multiple nodes](scale.md)).

Each live session is one Pi process (or guest). `max_sessions` counts
those live processes on the node. `max_sessions_per_tenant` counts them
for one tenant. `worker_memory_mb` is the RAM budget for the same live
guests. A new turn that would pass either node cap returns `429` with
code `capacity`. The session row in Postgres can outlive the process;
idle TTL kills the process and frees a slot.

| Failure | HTTP or event | Code |
| --- | --- | --- |
| Node live-session cap | `429` | `capacity` |
| Per-tenant live-session cap | `429` | `capacity_tenant` |
| Request body too large | `413` | `payload_too_large` |
| Workspace directory too large | `agent.session.error` | `workspace_too_large` |
| Artifact store too large | `agent.session.error` | `artifact_too_large` |

The gateway does not intercept every write inside a guest. Guest tmpfs
is already bounded by `[sandbox.resources].mem_mib`. Workspace and
artifact caps are enforced when the host unpacks or publishes. Host
files that are already on disk stay until workspace TTL.
`self_hosted` runner disk is not capped; bytes published onto the
gateway still count toward `max_artifact_bytes`.

Artifact metadata stays in Postgres. Bytes default to local files.
Set `artifact_store = "s3"` for any S3-compatible API. Hosted file and
skill bytes use the same setting. Production that serves those bytes
from more than one node should use S3. Put access keys
in the process environment (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`), not in TOML. Install the extra with
`uv sync --extra s3`.

```
APIPI_ARTIFACT_STORE=s3
APIPI_S3_BUCKET=apipi-artifacts
APIPI_S3_ENDPOINT=https://hel1.your-objectstorage.com
APIPI_S3_REGION=hel1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

The live `openai_hosted` workspace stays on the node. Published
artifact content, and hosted file and skill bytes, can be read from any
gateway process that shares the bucket.

## Pi

The `[pi]` table is the harness: the binary ApiPi execs and options
passed into that process. It is not bind address, Postgres, or
Firecracker.

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_PI_COMMAND` | `[pi].command` | `pi` | Pi binary used as `pi --mode rpc`. |
| `APIPI_PI_AUTO_COMPACT` | `[pi].auto_compact` | on | When off, ApiPi passes `--no-auto-compact` so Pi does not compact context on its own. |
| `APIPI_PLATFORM_PROMPT` | `[pi].platform_prompt` | built-in text | Main platform prompt appended after Pi's harness default. Unset keeps the built-in. Set to `""` to disable the main block. A non-empty value replaces the built-in entirely. |
| `APIPI_PLATFORM_PROMPT_ADDITIONAL` | `[pi].platform_prompt_additional` | empty | Optional extra platform text appended after the main block. Does not replace the main prompt. |

The gateway always composes those blocks before `agent.instructions`.
Order: Pi's default system prompt, main platform prompt, additional
platform prompt, then agent instructions. Skills, capability
directories, packages, and setup commands are unchanged. Empty main
(`platform_prompt = ""`) drops only the main block; additional and
agent instructions still apply. These settings live on the process
that runs Pi (combined `apipi serve` or `apipi worker`).

The built-in main prompt tells the model that hosted cwd is
`/workspace`, durable files go under `outputs/` only, `none` has no
computer, scratch is deleted with the sandbox, and it must not invent
unavailable APIs. The gateway also appends the resolved sandbox size
(`S` / `M` / `L`). Size `L` with Playwright attached adds a browser
block: system Chromium is already there, use MCP tools, do not
install browsers.

```toml
[pi]
command = "pi"
auto_compact = true
```

Override the main prompt, or keep it and append a sentence:

```toml
[pi]
platform_prompt = ""
platform_prompt_additional = "Always answer in German."
```

## Sandbox

The `[sandbox]` table is the isolation boundary: which backend runs Pi,
guest images, RAM and vCPUs, and TAP egress. Production SaaS and
enterprise set `backend = "microvm"` so each session is a Firecracker
guest. Isolation `none` is local and CI. Custom backends use an import
path. What to install and how systemd looks is in
[run modes](run-modes.md).

`microvm` and custom backends that set `needs_probe` launch a throwaway
sandbox before the API listens. If the mode cannot start, the process
exits. There is no silent fallback. `host` and `jail` are not valid.

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_RUN_MODE` | `[sandbox].backend` | `none` | `none` \| `chat` \| `microvm` \| `package.mod:Class`. `chat` is the same host backend as `none` with a distinct pool label. |
| `APIPI_MICROVM_KERNEL` | `[sandbox].kernel` | `$XDG_CACHE_HOME/apipi/microvm/vmlinux` when that file exists | Guest kernel image. Required when the backend is `microvm` unless `apipi install --microvm` has already written the cache file. |
| `APIPI_MICROVM_ROOTFS` | `[sandbox].rootfs` | `$XDG_CACHE_HOME/apipi/microvm/rootfs.ext4` when that file exists | Guest rootfs for `image = "default"`. Required when the backend is `microvm` unless the cache file exists. Build with `apipi install --microvm` or `./scripts/microvm-rootfs`. |
| `APIPI_MICROVM_ROOTFS_BROWSER` | `[sandbox].rootfs_browser` | `$XDG_CACHE_HOME/apipi/microvm/rootfs-browser.ext4` when that file exists | Guest rootfs for `image = "browser"`. Required when that image is selected unless the cache file exists. Build with `apipi install --microvm --image browser`. |
| `APIPI_MICROVM_IMAGE` | `[sandbox].image` | `default` | `default` \| `browser`. Used by `apipi install` and `apipi microvm shell`. Live session guests follow sandbox size (`S`/`M` → default rootfs, `L` → browser), not this process-wide setting. |
| `APIPI_SANDBOX_DEFAULT_SIZE` | `[sandbox].default_size` | `S` | `S` \| `M` \| `L`. Gateway default when the session does not set `environment.sandbox_size` or `metadata["apipi.sandbox_size"]`. `L` as default needs the browser rootfs and a RAM budget for ~2 GiB guests. Playwright MCP is injected on `L` unless you turn that off. |
| `APIPI_SANDBOX_AUTO_PLAYWRIGHT` | `[sandbox.browser].auto_playwright` | on | When on, size `L` on `microvm` injects Playwright MCP (system Chromium). Off keeps L RAM and rootfs but does not attach browser tools. |
| `APIPI_SANDBOX_PLAYWRIGHT_MCP` | `[sandbox.browser].playwright_mcp` | `@playwright/mcp@latest` | npm package passed to `npx -y` for the injected server. Pin a version for reproducible guests. |

```toml
[sandbox]
backend = "microvm"
kernel = "/var/lib/apipi/vmlinux"
rootfs = "/var/lib/apipi/rootfs.ext4"
rootfs_browser = "/var/lib/apipi/rootfs-browser.ext4"
image = "default"
default_size = "S"

[sandbox.browser]
auto_playwright = true
```

```
APIPI_RUN_MODE=microvm uv run apipi serve
```

### Resources

Guest RAM and vCPUs belong to the sandbox, not to the HTTP process.
Set `worker_memory_mb` to usable host RAM minus reserve. Size
`max_sessions` so packed guests still fit in that budget; the
scheduler will not oversubscribe either cap. `S` uses `mem_mib`. `M`
and `L` use their own RAM settings. `L` is the browser-class size.
A worked example is in [production](production.md#sizing).

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_MICROVM_MEM_MIB` | `[sandbox.resources].mem_mib` | `512` | Guest RAM in MiB for size `S`. |
| `APIPI_SANDBOX_M_MEM_MIB` | `[sandbox.resources].m_mem_mib` | `1024` | Guest RAM in MiB for size `M`. |
| `APIPI_SANDBOX_L_MEM_MIB` | `[sandbox.resources].l_mem_mib` | `2048` | Guest RAM in MiB for size `L`. |
| `APIPI_MICROVM_VCPUS` | `[sandbox.resources].vcpus` | `1` | Guest vCPUs. |

```toml
[sandbox.resources]
mem_mib = 512
m_mem_mib = 1024
l_mem_mib = 2048
vcpus = 1
```

### Networking

MicroVM TAP egress is open to the public internet by default and
capped at 50 Mbit with `tc`. Guest localhost (loopback inside the
guest) works. The model host from `OPENAI_BASE_URL` is always
reachable, including when you turn the optional destination allowlist
on. The guest cannot use **host** loopback, so it cannot open Postgres
on the worker's `localhost`.

To lock destinations, set `egress_allowlist = true`. Then the guest
may reach only the model host, this session's HTTP MCP hosts, extra
`egress_hosts`, package registries when `environment.packages` is set,
and DNS. Unlisted TCP is rejected. Session `environment.network` can
still disable TAP egress or restrict it to named hosts. A session
cannot add a host that this allowlist forbids. If the allowlist is
off, a session may still set `disabled` or `restricted`. Isolation
`none` cannot enforce that field.

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_MICROVM_EGRESS_ALLOWLIST` | `[sandbox.network].egress_allowlist` | off | Optional fail-closed TAP allowlist when the backend is `microvm`. |
| `APIPI_MICROVM_EGRESS_HOSTS` | `[sandbox.network].egress_hosts` | empty | Extra hostnames when the allowlist is on, comma-separated or a TOML array. |
| `APIPI_MICROVM_EGRESS_MBIT` | `[sandbox.network].egress_mbit` | `50` | `tc` rate on each guest TAP, both directions. Always on. |

```toml
[sandbox.network]
egress_allowlist = false
egress_mbit = 50
```

Lock down to named hosts (model host is still included):

```toml
[sandbox.network]
egress_allowlist = true
egress_hosts = ["mcp.tavily.com"]
egress_mbit = 50
```

### Lifetime

```toml
[sandbox.ttl]
openai_hosted = "1h"
self_hosted = "0"
```

## Dev and production files

A local checkout can use TOML for structure and `.env` for secrets:

```toml
# apipi.toml
database_url = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
host = "0.0.0.0"
port = 8000
max_sessions = 8

[pi]
command = "pi"

[sandbox]
backend = "none"
```

```
# .env
OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_API_KEY_OVERWRITE=...
```

A production microVM host looks like this. Keep
`OPENAI_API_KEY_OVERWRITE` (if you use it) and any export tokens in
`/etc/apipi.env`, not in the committed TOML file:

```toml
database_url = "postgresql+asyncpg://apipi:apipi@postgres:5432/apipi"
host = "0.0.0.0"
port = 8000
instance_id = "node-a"
max_sessions = 32
max_sessions_per_tenant = 8
worker_memory_mb = 16384
auth = "mycompany.apipi_auth:authenticate"

[pi]
command = "pi"
auto_compact = true

[sandbox]
backend = "microvm"
kernel = "/var/lib/apipi/vmlinux"
rootfs = "/var/lib/apipi/rootfs.ext4"
rootfs_browser = "/var/lib/apipi/rootfs-browser.ext4"
image = "default"
default_size = "S"

[sandbox.resources]
mem_mib = 512
m_mem_mib = 1024
l_mem_mib = 2048
vcpus = 1

[sandbox.network]
egress_allowlist = false
egress_mbit = 50

[sandbox.ttl]
openai_hosted = "1h"
self_hosted = "0"

[sandbox.browser]
auto_playwright = true

[placement]
env_none = "chat"
```

## Compatibility

Environment variable names are unchanged (`APIPI_RUN_MODE`,
`APIPI_PI_COMMAND`, `APIPI_MICROVM_MEM_MIB`, and the rest). Flat TOML
keys such as `run_mode` and `microvm_mem_mib` still load for this
release and log a deprecation warning that names the nested path. Do
not set a flat key and its nested path in the same file. The next
release will reject the flat keys as unknown.
