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
| `APIPI_IDLE_TTL` | `idle_ttl` | `15m` | Idle timer for `none` and `self_hosted` sessions. Kills Pi to free RAM. Hosted computers use the sandbox TTL instead. This follows environment type, not `APIPI_RUN_MODE`. The process that holds Pi runs the timer: combined `apipi serve`, or `apipi worker` in a split deploy. An agent or session `idle_ttl` overrides it. |
| `APIPI_SANDBOX_TTL_OPENAI_HOSTED` | `[sandbox.ttl].openai_hosted` | `1h` | Idle timer for an `openai_hosted` computer. One timer stops Pi and deletes the workspace together. There is no separate guest timeout. Transcript and published artifacts stay. `0` turns the timer off. `APIPI_WORKSPACE_TTL` / `workspace_ttl` is an alias. An agent or session `idle_ttl` overrides it. |
| `APIPI_SANDBOX_TTL_SELF_HOSTED` | `[sandbox.ttl].self_hosted` | `0` (off) | Not used by the Pi idle reap. `self_hosted` Pi uses `APIPI_IDLE_TTL` (or an agent or session override). The gateway cannot delete files on the runner. `0` means off. |
| `APIPI_MAX_SESSIONS` | `max_sessions` | `32` | Live Pi processes on this node. A new turn that would pass the cap returns `429` with code `capacity`. Idle reap frees a slot. Postgres session rows are not counted. Workers advertise this as `capacity`. |
| `APIPI_MAX_SESSIONS_PER_TENANT` | `max_sessions_per_tenant` | `32` | Live Pi processes for one tenant. A new turn that would pass the cap returns `429` with code `capacity_tenant`. The node cap still applies. |
| `APIPI_WORKER_MEMORY_MB` | `worker_memory_mb` | `max_sessions × mem_mib` (16384 at defaults) | RAM budget this worker (or combined node) will run, in MiB. Sum of guest `mem_mib` for live leases must stay under this. Set it to usable host RAM minus OS and worker reserve. Do not read `/proc/meminfo` automatically. |
| `APIPI_TURN_TIMEOUT` | `turn_timeout` | `10m` | Fail a stuck turn with code `turn_timeout`. This is not a user cancel. |
| `APIPI_ERROR_CODES` | `error_codes` | `legacy` | `legacy` or `specific`. `legacy` keeps `model_host_error` on `agent.session.error` and the non-stream `502` body for upstream failures. The specific code is `detail_code`. `specific` puts that code in `code` now. `turn.failed`, logs, and usage always use the specific code. See [failure codes](errors.md). |
| `APIPI_AUTH` | `auth` | unset (default hash) | Import path `package.mod:func` for the auth callback. The callback may return a typed reject (`401` or `429`). |
| `APIPI_WORKER_TOKEN` | `worker_token` | unset | Shared secret for `apipi worker` connections. Compared in memory. Not a tenant key and not stored in the database. Unset rejects the worker socket. Put this in the process environment. See [workers](workers.md). |
| `APIPI_VAULT_MASTER_KEY` | `vault_master_key` | local default | 32-byte AES-256-GCM key for MCP vault tokens at rest (standard or urlsafe base64, or 64-char hex). Unset uses a local default so laptop try-outs keep working, and logs a warning. Production must set a real key from the deploy secret store. Never commit it. `apipi migrate` rewrites leftover plaintext rows to ciphertext. Generate with `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"`. |
| `APIPI_WORKER_LEASE_TTL` | `worker_lease_ttl` | `30s` | How long a session lease stays valid without a heartbeat. Expiry fails closed and emits `agent.session.error` with code `worker_lease_expired`. |
| `APIPI_API_URL` | `api_url` | unset (`http://127.0.0.1:8000` for `apipi worker`) | Base URL the worker uses to open `/internal/worker`. |
| `APIPI_API_ONLY` | `api_only` | off | Control plane only. Turns lease a worker. `apipi serve --api-only` sets this. |
| `APIPI_ENV_NONE_PLACEMENT` | `[placement].env_none` | `chat` | Where Agents sessions with `environment.type=none` run on a mixed fleet: `chat` (chat workers), `microvm` (legacy computer workers), or `reject` (`400` code `placement`). Session metadata `apipi.session_kind=chat` always uses chat workers. Computer environments always use `microvm`. See [chat fleets](chat.md) and [workers](workers.md). |
| `APIPI_AUTH_CACHE_TTL` | `auth_cache_ttl` | `30s` | Cache success and `401` rejects by SHA-256 of the bearer, never the raw key. `429` rejects are not cached. |
| `APIPI_SESSIONS_DIR` | `sessions_dir` | `.apipi/sessions` under cwd | Root for local session directories (`openai_hosted`). Must be writable by the gateway user. Local artifacts live under `.artifacts` there. A leftover root-owned tree fails harvest with code `artifact_store`. |
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
| `APIPI_S3_ADDRESSING` | `s3_addressing` | `auto` | `auto` \| `path` \| `virtual`. `auto` is virtual-hosted (`bucket.endpoint/key`). Set `path` for R2 or MinIO on an IP. Guest images fall back to this endpoint, region, and addressing when `APIPI_IMAGE_S3_*` is unset. The image bucket and prefix come from the image URI, not from `s3_bucket` or `s3_prefix`. Artifact credentials come from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` or the instance role, never from TOML. |
| `APIPI_PRESIGN_TTL` | `presign_ttl` | `15m` | Lifetime of presigned PUT/GET URLs. Needs `artifact_store=s3`. |
| `OPENAI_BASE_URL` | `model_base_url` | required for serve | Model host passed to Pi. Not the gateway URL. Put this in `.env`. |
| `OPENAI_API_KEY_OVERWRITE` | `model_api_key_overwrite` | unset | Optional operator model key. When unset, Pi gets the request bearer. A process `OPENAI_API_KEY` is ignored. |
| `APIPI_FORWARD_MODELS` | `forward_models` | on | Proxy `GET /v1/models` to `{OPENAI_BASE_URL}/models` when `APIPI_MODEL_LIST` is `probe` or `turn`. Off returns `400` with code `forward_models`. With `APIPI_MODEL_LIST=off`, the route returns the static `APIPI_MODELS` list instead of calling the host. |
| `APIPI_MODEL_LIST` | `model_list` | `probe` | `probe` \| `turn` \| `off`. Checked when an agent is created or its model is edited, not on turns. `probe` lists `{OPENAI_BASE_URL}/models` at startup and reuses that list. `turn` lists on each agent write and does not list at startup. `off` never calls `/models`. |
| `APIPI_MODELS` | `models` | empty | Comma-separated model ids, or a TOML list. Used when `APIPI_MODEL_LIST=off`. An empty list skips the check. |
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
| `APIPI_LIFECYCLE_EXPORT_URL` | `lifecycle_export_url` | unset | HTTPS POST of session live start, stop, and heartbeat batches. Off when unset and `APIPI_LIFECYCLE_SINKS` is empty. See [usage](usage.md#session-lifecycle-export). |
| `APIPI_LIFECYCLE_EXPORT_TOKEN` | `lifecycle_export_token` | unset | Bearer for the lifecycle export URL. Put this in the process environment. |
| `APIPI_LIFECYCLE_EXPORT_TIMEOUT` | `lifecycle_export_timeout` | `5s` | HTTP timeout, and the shutdown flush limit. |
| `APIPI_LIFECYCLE_SINKS` | `lifecycle_sinks` | empty | Extra lifecycle sinks, comma-separated `package.mod:Class`. Each sink implements `emit(event)`. |
| `APIPI_LIFECYCLE_HEARTBEAT` | `lifecycle_heartbeat` | `60s` | How often the pool owner posts its live set. `0` or `off` disables heartbeats. |
| `APIPI_LIFECYCLE_QUEUE` | `lifecycle_queue` | `10000` | Max queued lifecycle events. A full queue drops the new event. |
| `APIPI_LIFECYCLE_BATCH` | `lifecycle_batch` | `100` | Max events in one HTTP POST. |
| `APIPI_LIFECYCLE_BATCH_WAIT` | `lifecycle_batch_wait` | `1s` | Flush a short batch after this wait. |
| `APIPI_LIFECYCLE_RETRY_MAX` | `lifecycle_retry_max` | `60s` | Cap for exponential backoff on 5xx, 429, 408, and network errors. |
| `APIPI_LIFECYCLE_USER_ID` | `lifecycle_user_id` | `raw` | `raw`, `hash`, or `omit`. `hash` needs `APIPI_LIFECYCLE_USER_ID_KEY`. |
| `APIPI_LIFECYCLE_USER_ID_KEY` | — | unset | HMAC key for `hash`. Process environment only. Required when `APIPI_LIFECYCLE_USER_ID=hash`. |
| `APIPI_LIFECYCLE_RUN_MODES` | `lifecycle_run_modes` | empty | Comma-separated run modes that emit lifecycle events. Empty means all. |
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
idle TTL kills the process and frees a slot. In a split deploy the
worker runs that reap, not the API. The timer is chosen by environment
type, not by run mode. `none` and `self_hosted` use `APIPI_IDLE_TTL`.
`openai_hosted` uses the sandbox TTL, and that one timer covers Pi and
the guest together. A session `idle_ttl`, then the agent `idle_ttl`,
then that default. `0` on an override turns the timer off for that
session. `APIPI_SANDBOX_TTL_SELF_HOSTED` does not kill Pi.

| Failure | HTTP or event | Code |
| --- | --- | --- |
| Node live-session cap | `429` | `capacity` |
| Per-tenant live-session cap | `429` | `capacity_tenant` |
| Request body too large | `413` | `payload_too_large` |
| Workspace directory too large | `agent.session.error` | `workspace_too_large` |
| Artifact store too large | `agent.session.error` | `artifact_too_large` |
| Artifact store not writable, including S3 errors | `agent.session.turn.failed` | `artifact_store` |
| Artifact store error on an HTTP read or upload | `503` | `artifact_store` |
| Missing `agent.model` on session create | `400` | `model_required` |
| Unknown model on agent create or edit | `400` | `model_not_found` |
| Model list unreachable on agent write, including `404` | `400` | `model_host_unreachable` |
| Model host rejects the key on agent write (`401` or `403`) | `401` | `model_host_unauthorized` |
| Host rejects the model during a turn | `agent.session.turn.failed` | specific upstream code (`model_host_error` on `agent.session.error` while `APIPI_ERROR_CODES=legacy`) |

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

Presigned uploads (`POST /v1/uploads`) send bytes straight to the bucket.
The browser never holds the ApiPi API key. Virtual-hosted URLs match
Hetzner (`https://bucket.hel1.your-objectstorage.com/…`). Set a CORS
rule on the bucket that allows `PUT`, `GET`, and `HEAD` from your SPA
origin, including the `Content-Type` header. A presigned GET forces
`Content-Disposition: attachment` with the original file name, and an
RFC 5987 `filename*` when that name is not ASCII. Active content such
as HTML, SVG, XML, and JavaScript is never served inline: those objects
are signed as `application/octet-stream`. Local `artifact_store`
returns `400` with code `presign_unsupported`. An S3 or botocore
failure while writing artifacts fails the turn with code
`artifact_store`, the same code as a local `OSError`. A missing object
is not that error. If a Pi session cache is already stored and the
read fails, the turn fails with `artifact_store` instead of starting
without the cache. Session create that cannot read a hosted file or
skill returns `503` with code `artifact_store`. The same store error
on a gateway read or upload returns `503` with that code.

The live `openai_hosted` workspace stays on the node. Published
artifact content, and hosted file and skill bytes, can be read from any
gateway process that shares the bucket.

## Model host

`OPENAI_BASE_URL` is an OpenAI-compatible HTTP host. Pi in the guest
calls that URL. It is not the gateway URL. The request bearer, or
`OPENAI_API_KEY_OVERWRITE` when that is set, is sent as
`Authorization: Bearer`.

`POST {OPENAI_BASE_URL}/chat/completions` is required. Pi uses
`api: openai-completions`. A non-streaming response has
`choices[0].message.content`. A streaming response is SSE
`chat.completion.chunk` events. Tool calls use the OpenAI
`tool_calls` shape on the assistant message. ApiPi does not call
`POST {OPENAI_BASE_URL}/embeddings`. That route is not required.

`GET {OPENAI_BASE_URL}/models` is required only when `APIPI_MODEL_LIST`
is `probe` or `turn`, and when `GET /v1/models` forwards. The body is
`{"object": "list", "data": [{"id": "<model>"}]}`. Only `data[].id` is
read. A host `401` or `403` becomes `model_host_unauthorized`. Any
other failure, including `404`, becomes `model_host_unreachable`.
Agent create and model edit return that error directly. Turns do not
call `/models`.

`probe` is the default. Startup lists once. Agent writes reuse that
list. `turn` lists on each agent write, not on each conversation turn.
`off` is for a host with no `/models`. Set `APIPI_MODELS` to the ids
you allow, or leave it empty to skip the check. `apipi serve` and
`apipi worker` still require `OPENAI_BASE_URL` and the pinned Pi.
They do not call `/models` in `off` or `turn`.

### Failure modes

There are three outcomes. Do not treat them as the same error.

An HTTP error rejects the request. Pi does not start. `POST /v1/agents`
and a model edit return `400` with `model_not_found` when the id is
not in the list, `400` with `model_host_unreachable` when `GET /models`
fails (including `404`), and `401` with `model_host_unauthorized` when
the host returns `401` or `403`. The agent row is not written. Session
create with a non-empty input and no `agent.model` returns `400` with
`model_required` before Pi starts. The session row may already exist.
`GET /v1/models` uses the same host codes when it proxies.
`APIPI_FORWARD_MODELS=off` returns `400` with code `forward_models`
and does not call the host.

A turn failure happens after Pi has started. The host can reject the
model for any reason: it is missing, overloaded, or the key is bad.
ApiPi classifies Pi's `errorMessage` into a specific code, a
`failure_source`, an `upstream_status` when the text contains one, and
`retryable`. The parser matches the pinned Pi version and is
best-effort: Pi does not send the HTTP status as a field. A secret in
the message is still masked. `agent.session.turn.failed` carries the
specific code. In this release `agent.session.error` and the
non-stream `502` body keep `model_host_error` for upstream failures,
with the specific code in `detail_code`. Set `APIPI_ERROR_CODES=specific`
to opt in early. The session returns to `idle`, so a follow-up message
can try again. An artifact-store failure during the turn uses
`artifact_store` and `failure_source` `internal`. A Pi process that
exits before the turn settles is `pi_exited`. A host Pi killed for
memory is `pi_memory`. A turn that exceeds `turn_timeout` is
`turn_timeout`, not a cancel. The full list is in
[failure codes](errors.md).

A session failure is terminal. Status becomes `failed`. The events are
`agent.session.error` (with `code` and `message`) and then
`agent.session.failed`. A worker turn that still has no model uses
code `model_required`. An unexpected exception on `turn.start` or
`turn.continue` uses the `ApiError` code, or `internal` when it is
not an `ApiError`. The worker logs `worker.command.failed` at the
same level as a turn failure: warning for caller errors, error for
internal faults, with `session_id`, `tenant_id`, and `request_id`. The
task does not raise again, so the client is not left waiting on a
silent turn. If the session is already `failed`, that log is the only
extra record.

`probe` startup is not a turn error. If `GET /models` fails, `apipi
serve` and `apipi worker` exit before they listen. `turn` and `off`
do not call `/models` at start. A host without that route must use
`off`, or agent writes in `turn` mode fail with
`model_host_unreachable`.

## Pi

The `[pi]` table is the harness: the binary ApiPi execs and options
passed into that process. It is not bind address, Postgres, or
Firecracker.

| Env | TOML | Default | What |
| --- | --- | --- | --- |
| `APIPI_PI_COMMAND` | `[pi].command` | `pi` | Pi binary used as `pi --mode rpc`. |
| `APIPI_PI_AUTO_COMPACT` | `[pi].auto_compact` | on | When off, ApiPi writes `compaction.enabled` false in Pi `settings.json`. Pi 0.85.1 does not accept `--no-auto-compact`, so that flag is not passed. |
| `APIPI_PI_COMPACTION_RESERVE_TOKENS` | `[pi].compaction_reserve_tokens` | unset (Pi default 16384) | `compaction.reserveTokens` in Pi `settings.json`. Tokens reserved for the model reply. Unset leaves Pi's default. |
| `APIPI_PI_COMPACTION_KEEP_RECENT_TOKENS` | `[pi].compaction_keep_recent_tokens` | unset (Pi default 20000) | `compaction.keepRecentTokens` in Pi `settings.json`. Recent tokens kept out of the summary. Unset leaves Pi's default. |
| `APIPI_PI_THINKING` | `[pi].thinking` | `off` | Process default thinking level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`. A session or agent may override it. |
| `APIPI_PI_MEM_MIB` | `[pi].mem_mib` | unset | Soft ceiling for one host Pi (`none` / `chat`) in MiB. Unset is off. Sets Node `NODE_OPTIONS=--max-old-space-size` and kills the process group when RSS goes over the limit (`apipi_pi_kill_total` reason `memory`). A turn in progress fails with `pi_memory`. Not a microVM hard cap. |
| `APIPI_PI_SYSTEM_PROMPT` | `[pi].system_prompt` | unset | Replaces Pi's harness default system prompt. Unset or empty keeps Pi's default. This does not replace the platform prompt, agent instructions, context files, or skills. It does drop Pi's tool list and all tool guidelines, including MCP and Playwright guidance. The tools stay callable. |
| `APIPI_PLATFORM_PROMPT` | `[pi].platform_prompt` | built-in text | Main platform prompt appended after Pi's harness default (or after `system_prompt` when that is set). Unset keeps the built-in. Set to `""` to disable the main block. A non-empty value replaces the built-in entirely. |
| `APIPI_PLATFORM_PROMPT_ADDITIONAL` | `[pi].platform_prompt_additional` | empty | Optional extra platform text appended after the main block. Does not replace the main prompt. |
| `APIPI_MODEL_RETRY_ENABLED` | `[pi].model_retry_enabled` | on | Pi `retry.enabled`. When on, Pi retries a failed model call. ApiPi does not retry the turn. |
| `APIPI_MODEL_MAX_RETRIES` | `[pi].model_max_retries` | `3` | Pi `retry.maxRetries`. Retries after the first attempt. |
| `APIPI_MODEL_BACKOFF_BASE_MS` | `[pi].model_backoff_base_ms` | `2000` | Pi `retry.baseDelayMs`. Delay is `base × 2^(attempt-1)`. |
| `APIPI_MODEL_BACKOFF_MAX_MS` | `[pi].model_backoff_max_ms` | `30000` | No Pi key. ApiPi lowers `retry.maxRetries` so the last delay stays at or under this cap, and logs that. |
| `APIPI_MODEL_TIMEOUT_MS` | `[pi].model_timeout_ms` | `120000` | Pi `httpIdleTimeoutMs` and `retry.provider.timeoutMs`. Idle timeout for a hung call. Must be at least 1. `0` is not allowed. |
| `APIPI_MODEL_PROVIDER_RETRIES` | `[pi].model_provider_retries` | `0` | Pi `retry.provider.maxRetries`. Silent HTTP retries. Default `0` so they do not multiply session retries. |
| `APIPI_MODEL_RETRY_AFTER_MAX_MS` | `[pi].model_retry_after_max_ms` | `30000` | Pi `retry.provider.maxRetryDelayMs`. A `Retry-After` above this fails that provider retry. Session retry may still run. |

Thinking stays off until the resolved level is not `off`. ApiPi then
passes `--thinking` to Pi, writes `defaultThinkingLevel` in
`settings.json`, and writes each model in the session `models.json`
with `reasoning` true and `supportsReasoningEffort` true. That session
file uses the resolved level, not only the process default, so a
session or agent level still marks reasoning when
`APIPI_PI_THINKING=off`. That asks an
OpenAI-compatible host for `reasoning_effort`. Hosts that need another
Pi thinking format, such as `chat-template` or `qwen`, are not
configured here. `xhigh` and `max` are passed through. Pi drops a
level the model does not support. `off` leaves `models.json` as it is
today and does not pass `--thinking`. The process default is
`[pi].thinking`. A session may set `metadata["apipi.thinking"]`. A
saved agent may set the same key. Resolve order is session, then
agent, then the process default. Inline agents copy that key onto the
session when the session did not set it. The level is applied when Pi
starts. A later change respawns Pi. Public events then carry a preview
of the first 100 Unicode code points, a duration, and a reasoning token
count. The full thinking text is not a public event. See
[events](api.md#events).

Compaction and the system prompt are written into the session Pi agent
directory before Pi starts (`settings.json` and, when set, `SYSTEM.md`).
That directory is `PI_CODING_AGENT_DIR`. Pi 0.85.1 reads global settings
and `SYSTEM.md` from there. Project `.pi/settings.json` and
`.pi/SYSTEM.md` are not used, because RPC does not trust the workspace.
`compaction.enabled` follows `[pi].auto_compact`. Thresholds are written
only when set. Those compaction settings stay process-wide.

Model retry settings are always written, so Pi does not use its own
defaults. `retry.enabled`, `retry.maxRetries`, `retry.baseDelayMs`,
`retry.provider.maxRetries`, `retry.provider.maxRetryDelayMs`,
`retry.provider.timeoutMs`, and `httpIdleTimeoutMs` come from the
`APIPI_MODEL_*` settings. They are process-wide. There is no per-agent
override in this version.

Pi 0.85.1 has no session-level backoff cap, so
`APIPI_MODEL_BACKOFF_MAX_MS` limits the effective `retry.maxRetries`
instead. It also has no status-based selection at that layer: retry
matching is text. A `400` whose body mentions `timeout` or `502` can
match and be retried. `Retry-After` is honoured only by the provider
layer, and a delay above the cap fails that layer instead of waiting
the cap. Provider retries emit no events, so they are not in
`upstream_attempts`. The timeout is an idle timeout for the process,
not a wall-clock limit on one request. `APIPI_TURN_TIMEOUT` remains
the hard limit.

At startup ApiPi warns when
`timeout × (max retries + 1) + backoff` exceeds `APIPI_TURN_TIMEOUT`.
The defaults are about 8.2 minutes, under the 10 minute turn timeout.
With provider retries above `0`, total HTTP tries can be up to
`(provider retries + 1) × (max retries + 1)`. That product is not in
the startup budget.

The gateway always composes the appended blocks before
`agent.instructions`. Order: Pi's harness default, or
`system_prompt` when that is set (session metadata, then agent
metadata, then `[pi].system_prompt`); then the main platform prompt;
then additional platform text; then, only for a hosted microvm
computer, a size line and an optional network line. Then
`agent.instructions`. The gateway does not add Playwright text from
the injected tool list. Those names appear only after the guest
attach succeeds, inside the Pi extension. Skills, capability
directories, packages, and setup commands are unchanged. Empty main
(`platform_prompt = ""`) drops only the main block; additional and
agent instructions still apply. An empty `system_prompt` keeps Pi's
harness default. Platform prompt and compaction settings live on the
process that runs Pi (combined `apipi serve` or `apipi worker`).
Thinking level and system prompt may also be set per session.

The built-in main prompt matches the session. A hosted computer is
told that the working directory is `/workspace` and that durable
files go under `outputs/`. A self-hosted computer is told that the
working directory is the runner's files. Chat and `environment.type`
`none` are told there is no computer and no file or shell tools.
They are not told about `/workspace`, sandbox size, or a browser.

| Fragment | When it is appended |
| --- | --- |
| No-computer main prompt | Chat, or `environment.type` is `none` or omitted. No `/workspace`, size, or browser text. |
| Hosted main prompt | `openai_hosted` (and the `hosted` alias) and not chat. Names `/workspace` and `outputs/`. |
| Self-hosted main prompt | `self_hosted` and not chat. Names the runner's files and `outputs/`. Does not mention `/workspace`. |
| Operator main prompt | `APIPI_PLATFORM_PROMPT` is set. Replaces the built-in main block. `""` drops it. |
| Additional platform text | `APIPI_PLATFORM_PROMPT_ADDITIONAL` is non-empty. Always, after the main block. |
| Size | Hosted microvm only. `Sandbox size is L (2048 MiB).` RAM comes from the size setting, not a hardcoded "2 GiB". No image name and no Chromium claim. |
| Network | Hosted microvm only, and only when `network.access` is `enabled` or `restricted`. Omitted when unset or `disabled`. |
| Playwright tool guidelines | Only after that server's tools register in the guest. One long guideline per server, not once per tool. The Chromium path is included only when that server's args name `/usr/bin/chromium-browser`. Not present when a replacement system prompt is set. |
| Bash install block | Blocks `npm install playwright` and `playwright install` only after Playwright tools have registered. Otherwise the command is allowed. This is a tool-call hook, not prompt text, so a replacement system prompt does not remove it. |
| `system_prompt` / skills | Operator or caller owned. A replacement system prompt keeps the platform blocks, instructions, context files, and skills. It removes Pi's tool list and all tool guidelines, including the MCP and Playwright rows above. |

```toml
[pi]
command = "pi"
auto_compact = true
compaction_reserve_tokens = 16384
compaction_keep_recent_tokens = 20000
```

Override the main prompt, or keep it and append a sentence:

```toml
[pi]
platform_prompt = ""
platform_prompt_additional = "Always answer in German."
```

### Context files

Pi 0.85.1 also loads context files into the prompt. This is a supported
way to add instructions for one session. ApiPi does not write these
files. A template, the caller, or the agent does.

Pi looks in the Pi agent directory first. That directory is
`<workspace>/.pi/agent`. In a microvm it is
`/workspace/.pi/agent`. It then looks in the working directory and
every parent directory. In each directory the first file that exists
wins, in this order: `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`,
`CLAUDE.md`, `CLAUDE.MD`.

Those files are appended after the platform text and
`agent.instructions`, and before skills. Pi loads them when it starts.
Files already in the workspace, including `environment.files`, are part
of that start. A file written during a turn does not change the prompt
that is already running. It does change the prompt of the next Pi start
in that session. That is intended. On microvm the next boot packs the
stored session directory. A file that exists only on the guest tmpfs
is not in that directory after a sandbox stop, so it is not loaded then.

A replacement system prompt does not drop context files. It still drops
Pi's tool list and tool guidelines, as the fragment table says.
[Issue 388](https://github.com/GEKI-AI/apipi/issues/388) revises the
fragment names in that table.

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
| `APIPI_MICROVM_ROOTFS` | `[sandbox].rootfs` | `$XDG_CACHE_HOME/apipi/microvm/rootfs.ext4` when that file exists | Guest rootfs for `image = "default"`. Required when the backend is `microvm` unless the cache file exists. Build with `apipi install --microvm`, `./images/build.sh default`, or `./scripts/microvm-rootfs`. |
| `APIPI_MICROVM_ROOTFS_BROWSER` | `[sandbox].rootfs_browser` | `$XDG_CACHE_HOME/apipi/microvm/rootfs-browser.ext4` when that file exists | Guest rootfs for `image = "browser"`. Required when that image is selected unless the cache file exists. Build with `apipi install --microvm --image browser`. |
| `APIPI_IMAGE_SOURCE` | `[sandbox].image_source` | unset | `s3://bucket/prefix`, `https://host/path`, or `file:///path`. Directory that holds `index.json`. `apipi images push` uses this when `--to` is omitted. |
| `APIPI_IMAGE_S3_ENDPOINT` | `[sandbox].image_s3_endpoint` | `APIPI_S3_ENDPOINT` | S3 endpoint for the guest image store. Unset uses the artifact endpoint. |
| `APIPI_IMAGE_S3_REGION` | `[sandbox].image_s3_region` | `APIPI_S3_REGION` | Region for the guest image store. Unset uses the artifact region. |
| `APIPI_IMAGE_S3_ADDRESSING` | `[sandbox].image_s3_addressing` | `APIPI_S3_ADDRESSING` | `auto` \| `path` \| `virtual` for the guest image store. Unset uses the artifact addressing. |
| `APIPI_IMAGE_S3_ACCESS_KEY_ID` | env only | unset | Access key for the image store. Set it with `APIPI_IMAGE_S3_SECRET_ACCESS_KEY`. Not a TOML key. |
| `APIPI_IMAGE_S3_SECRET_ACCESS_KEY` | env only | unset | Secret key for the image store. Not a TOML key. |
| `APIPI_IMAGE_S3_PROFILE` | env only | unset | AWS profile for the image store. Do not set this and the access keys together. Not a TOML key. If none of the image credential vars is set, the process uses the standard AWS credential chain. |
| `APIPI_IMAGES_DIR` | `[sandbox].images_dir` | `$XDG_CACHE_HOME/apipi/images` | Local images directory. Root uses the same home rule as the MicroVM cache, so `sudo apipi install` and the worker agree. |
| `APIPI_SANDBOX_IMAGES` | `[sandbox].images` | unset (every id in the index) | Image ids this host pulls and serves. |
| `APIPI_MICROVM_IMAGE` | `[sandbox].image` | `default` | `default` \| `browser` \| `work`. Used by `apipi install` and `apipi microvm shell`. Live session guests follow `sandbox_image`, not this process-wide setting. When the image is omitted, size `L` selects `browser` and other sizes use the default image. Explicit `kernel` / `rootfs` / `rootfs_browser` override the images dir for `default` and `browser` only. Other ids, including `work`, come from the images dir. Resolution is explicit path, then `<id>/current` in the images dir, then the legacy `~/.cache/apipi/microvm` files for `default` and `browser`. |
| `APIPI_SANDBOX_DEFAULT_IMAGE` | `[sandbox].default_image` | `default` | Guest image when the session does not set `environment.sandbox_image` or `metadata["apipi.sandbox_image"]`, and the size is not `L`. `L` still selects `browser`. This is not `APIPI_MICROVM_IMAGE`, which only selects the image for `apipi install` and `apipi microvm shell`. |
| `APIPI_SANDBOX_DEFAULT_SIZE` | `[sandbox].default_size` | `S` | `S` \| `M` \| `L`. Gateway default when the session does not set `environment.sandbox_size` or `metadata["apipi.sandbox_size"]`. `L` as default needs the browser rootfs and a RAM budget for ~2 GiB guests. Playwright MCP is injected when the image is `browser` unless you turn that off. Size `L` still selects that image when none is set. Install that rootfs with `apipi install --microvm --image browser`. |
| `APIPI_SANDBOX_AUTO_PLAYWRIGHT` | `[sandbox.browser].auto_playwright` | on | When on, image `browser` on `microvm` injects the vendored Playwright MCP server (system Chromium). Off keeps that image and its RAM but does not attach browser tools. |
| `APIPI_SANDBOX_EAGER_BOOT` | `[sandbox].eager_boot` | off | When on, creating an `openai_hosted` session starts the computer before the first turn. Off keeps the default: boot on the first turn. A session or agent `metadata["apipi.sandbox_eager_boot"]` overrides this. `on` or `off`. |

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
| `APIPI_MICROVM_VCPUS` | `[sandbox.resources].vcpus` | `1` | Guest vCPUs for sizes `S` and `M`. |
| `APIPI_SANDBOX_L_VCPUS` | `[sandbox.resources].l_vcpus` | `2` | Guest vCPUs for size `L`. |

```toml
[sandbox.resources]
mem_mib = 512
m_mem_mib = 1024
l_mem_mib = 2048
vcpus = 1
l_vcpus = 2
```

### Networking

MicroVM TAP egress may use the public internet by default and is
capped at 50 Mbit with `tc`. Private and special-use IPv4 ranges are
always rejected. That includes RFC1918 (`10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`), link-local (`169.254.0.0/16`, including cloud
metadata), shared address space (`100.64.0.0/10`), and loopback.
The guest TAP subnet stays open so Pi can reach the host broker.
Guest localhost (loopback inside the guest) works. The guest cannot
use **host** loopback, so it cannot open Postgres on the worker's
`localhost`. Pi reaches the model host through that broker, including
when the model host itself is on a private address. A session
allowlist cannot open a private range.

To lock destinations, set `egress_allowlist = true`. Then the guest
may reach only the model host, this session's HTTP MCP hosts, extra
`egress_hosts`, package registries when `environment.packages` is set,
and DNS, and only when those addresses are public. Unlisted TCP is
rejected. Private ranges stay rejected even if a name resolves to
one. Session `environment.network` can
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
# APIPI_VAULT_MASTER_KEY=...
```

A production microVM host looks like this. Keep
`OPENAI_API_KEY_OVERWRITE` (if you use it), `APIPI_VAULT_MASTER_KEY`,
and any export tokens in `/etc/apipi.env`, not in the committed TOML
file:

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
