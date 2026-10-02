# Production

How to choose hosts, scale out, and size an ApiPi process. Production
is `apipi serve --api-only` plus `apipi worker` on KVM. Combined
`apipi serve` is one box. Why that split exists is in
[workers](worker-concepts.md). Guest internals are in
[isolation](isolation.md).

## Host selection

Run production as `apipi serve --api-only` plus `apipi worker` on
KVM hosts with `APIPI_RUN_MODE=microvm` for computer sessions, plus
`APIPI_WORKER_ACCEPTS=none` workers where text-only sessions need
capacity. Combined `apipi serve` is the
single-host embedded worker. Nested Docker or nested KVM is a lab
setup. See [sandbox workers](workers.md#placement). The Compose file in this repo starts Postgres (and can run a
rootless API). `systemctl stop` / `restart` on `apipi worker` sends
SIGTERM. The worker heartbeats `"drain": true` (no new leases), waits
until live Pi are gone, then exits 0. Install
`deploy/systemd/apipi-worker-drain.conf` as a drop-in so
`TimeoutStopSec` covers that wait. Expired leases fail
closed; they are not reassigned. Set `APIPI_METRICS` and
`APIPI_OTEL_ENDPOINT` on the worker as well as the API so turn
series and turn/model spans are recorded where the turn runs. See
[usage](usage.md#prometheus).

Isolation `none` is for local machines and CI. If the selected mode
cannot start, `apipi serve` exits before it binds HTTP. The process
never switches to another mode on its own.

Size the box from **live** sessions, not from Postgres row counts.
Each live session is one Pi process or Firecracker guest. Guest RAM is
the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Firecracker VMM
overhead is small (~5 MiB per guest). Pi alone is modest. Chrome
inside the browser image needs hundreds of MiB extra, so raise
guest RAM rather than packing more 512 MiB guests.

Leave disk for per-worker `APIPI_SESSIONS_DIR` workspaces: each `openai_hosted` directory is
capped at `APIPI_MAX_WORKSPACE_BYTES` (default 1 GiB) and lasts until
sandbox TTL. Local artifact
bytes add up to `APIPI_MAX_ARTIFACT_BYTES` (default 512 MiB) per
session under the shared `APIPI_LOCAL_STORE_DIR` unless you set `APIPI_ARTIFACT_STORE=s3`.
Production split mode should use `s3`: the API issues presigned PUT and GET URLs,
the worker uploads and downloads directly, and store credentials exist only on the API.

Keeping the store off the worker host leaves more RAM for guests when
you use Postgres. One `apipi worker` (or one combined `apipi serve`)
per sandbox host; extra uvicorn workers leave the Pi pool in the first
worker only. One process can use SQLite. Several API processes share
Postgres. Give each process its own SQLite file if you are not sharing.

## Store

| Need | Why Postgres |
| --- | --- |
| Several gateway processes or nodes | SQLite is a single-writer file; share Postgres |
| Many concurrent writers on one DB | Single writer / lock |
| HA, backups, pooling at scale | Operator story |

## Model host

Point `OPENAI_BASE_URL` at an OpenAI-compatible host. Production should
use `APIPI_MODEL_LIST=probe` (the default). Serve and each worker list
`/models` once at start. Agent create and model edit use that list.
Turns do not call `/models`.

Use `APIPI_MODEL_LIST=off` when the host has no `GET /models`. Set
`APIPI_MODELS` to the ids you allow, or leave it empty to skip the
check. `turn` lists only when an agent is created or its model is
edited. If the host later rejects the model, the turn fails and the
session returns to `idle`. Alert on `failure_source=internal` and on
upstream `5xx`, timeouts, and connection errors. A `429` from the
host is retryable and is logged at warning, not error. In this release
the public error code is still `model_host_error`. A terminal failure
emits `agent.session.failed`. See
[failure codes](errors.md) and
[failure modes](config.md#failure-modes).

Only Pi retries a model call. The model host and this gateway do not
retry the turn. The gateway in front of the model host should fail
fast: a timeout, `504` with timeout text, `502` on a connection
error, `429` passed through (with `Retry-After` when it has one), and
`502` or `503` when the upstream is down. Pi times out a hung call,
retries those transient responses with bounded backoff, and does not
retry other `4xx`. ApiPi shows `agent.session.turn.retrying` while
that wait runs, and classifies the failure only after the last
attempt. Settings and the budget against `APIPI_TURN_TIMEOUT` are in
[Pi](config.md#pi).

## Guest image store

Mirror the official GitHub release store, verify it, then pull on
every worker. `apipi images mirror --from 0.12.0 --to s3://bucket/images`
copies one immutable `v<version>/` prefix. `apipi images verify` checks
the signature and digest chain. `apipi images pull` installs that
version. Rollback is `APIPI_IMAGE_STORE_VERSION` plus another pull.
Each image boots the kernel named in its manifest, so two store
versions can keep different kernels locally. A mirror that omits an
arch is partial: workers of the missing arch get a clear unavailable
error. Custom images still use `apipi images build` and
`apipi images push --store-version`. `--to` defaults to
`APIPI_IMAGE_SOURCE`. An `https://` static host is a valid source once
the files are there; push itself rejects `https://`.

The image store can use a different S3 endpoint and account than the
artifact store. Set `APIPI_IMAGE_S3_ENDPOINT`, `APIPI_IMAGE_S3_REGION`,
and `APIPI_IMAGE_S3_ADDRESSING` when they differ. Each falls back to
the matching `APIPI_S3_*` value when unset. Put image credentials in
the process environment: `APIPI_IMAGE_S3_ACCESS_KEY_ID` and
`APIPI_IMAGE_S3_SECRET_ACCESS_KEY`, or `APIPI_IMAGE_S3_PROFILE`. If
none of those is set, the worker uses the standard AWS credential
chain, the same chain as the artifact store. Never put those keys in
TOML. The artifact and session store keep using `APIPI_S3_*` and are
not affected by the image settings.

The official Images workflow publishes x86_64 `default` and `browser`
as GitHub release assets. It does not publish `work` or aarch64.
Build those locally if a worker needs them. It runs only from the
release tag. A release that does not change image inputs reuses the
previous store and signs it again. It does not rebuild the guest.
The 0.12.0 store was signed from `main`, so `verify` needs
`--no-signature` or `--signer-identity` for that tag. Use 0.12.1
instead.

## Scale-out

| Shape | When | What stays on the node | What is shared |
| --- | --- | --- | --- |
| Combined, one host | You fit in `max_sessions` on one box | Pi, SSE, WebSockets, `openai_hosted` directories, local artifacts | SQLite or Postgres |
| API-only + workers | Production. API in Docker or several replicas | Guests and workspaces on **workers**. API is stateless for Pi | Postgres, per-worker tokens, and artifact bytes through the store (`s3` recommended, or one shared `APIPI_LOCAL_STORE_DIR`) |
| Combined, several hosts | You have not split workers yet | Same as combined one host, plus each process has its own `APIPI_SESSIONS_DIR` | Postgres, auth callback. Sticky for live Pi. See [multiple nodes](scale.md) |
| External artifact store | Clients read artifacts from any API node | Live workspace still on the worker (or combined node) | Postgres, S3-compatible bucket |

With workers, `POST /v1/agents/sessions` and follow-up REST/SSE may
land on any API replica. The session is owned by the worker lease.
There is no live handoff of a running guest. Combined serve still
needs sticky routing for Pi. Examples are in
[multiple nodes](scale.md).

## Sizing

Count **live** Pi processes (or guests) on **workers**. The API
process is cheap next to guest RAM. Idle TTL (default 15 minutes)
kills the Pi process group on host workers (`none`) and
frees that RAM. The session row can outlive the process.
`max_sessions` and `worker_memory_mb` do not count Postgres rows.

```
worker_memory_mb ≈ (worker RAM − reserve)   # MiB, advertised as memory_mb
max_sessions ≈ worker_memory_mb / microvm_mem_mib
```

Reserve several GiB on each worker for the OS, jailer, and page cache.
Colocated Postgres needs more. Set `APIPI_WORKER_MEMORY_MB` from
**reserved** guest RAM rather than average guest RSS. The scheduler
will not start a guest that would pass either the RAM budget or
`max_sessions`. A new turn that cannot lease returns `429` with code
`capacity`.

### Example: 64 GiB RAM, 12 cores

Postgres on another host. Worker `APIPI_RUN_MODE=microvm`. Default
guest RAM 512 MiB and 1 vCPU. The API can be a small VM or a
container.

| | |
| --- | --- |
| Reserve | About **8 GiB** for OS, gateway, jailer, and page cache. |
| RAM budget | **`worker_memory_mb=57344`** (56 GiB). This is what the worker advertises as `memory_mb`. |
| Default 32 live | 32 × 512 MiB ≈ **16 GiB** guests plus ~0.2 GiB VMM. Fits easily. |
| Starting cap | **`max_sessions=48`** (24 GiB guests) or keep **32**. Raise after you watch host RSS and `429` `capacity`. The RAM cap still applies. |
| Ceiling | 57344 / 512 ≈ **112** live at 512 MiB. That is the wall, not a starting point. |
| Browser | Use sandbox size **`M`** or **`L`** with image `browser`. `M` is 1 GiB and `L` is 2 GiB. Both get at least 2 vCPUs. The guest runs agent-browser and chrome-headless-shell, not Playwright MCP. About **24–28** live `L` guests fit in a 56 GiB budget. `S` (512 MiB, 1 vCPU) is for Pi and light tools. The browser image is x86_64 only. |
| CPU | 48 × 1 vCPU on 12 cores is normal for `S` and non-browser `M` while turns wait on the model URL. Size `L` defaults to 2 vCPUs (`APIPI_SANDBOX_L_VCPUS`). Browser guests, including size `M`, also get at least 2. Keep `APIPI_MICROVM_VCPUS=1` for `S` and non-browser `M` unless that computer is CPU-heavy. |
| Disk | Hosted workspaces last until sandbox TTL (default 1 hour), capped at 1 GiB each. Local artifacts 512 MiB per session unless S3. Worst case is cap × live-and-idle directories, not typical use. |
| NIC | Each guest TAP is 50 Mbit. 48 guests all saturated ≈ 2.4 Gbit. That is the ceiling, not the plan. |

On a shared node set `APIPI_MAX_SESSIONS_PER_TENANT` lower than the
node cap (for example 8). A tenant that would pass it gets `429` with
code `capacity_tenant`.

A session still costs a Pi guest on this host. Hosted workspace disk is
elsewhere and is not capped here.

## Overprovision

| Resource | Overprovision? | Why |
| --- | --- | --- |
| Guest RAM | **No.** Set `worker_memory_mb` so the sum of guest `mem_mib` plus the host reserve fits. Size `max_sessions` as a second hard cap. | Each live session is a Firecracker guest with that RAM. There is no balloon device. The guest kernel usually touches the memory. The scheduler will not oversubscribe RAM or session count. |
| CPU | **Yes.** Default 1 vCPU for `S` and `M`, 2 for `L`. | Turns mostly wait on the model URL. Watch host load, not the vCPU count. Size `L` already uses 2 vCPUs for browser work. Raise `microvm_vcpus` or `l_vcpus` only if the computer is still CPU-heavy. |
| Disk | Caps are maxima, not reservations. | `max_workspace_bytes` and `max_artifact_bytes` are per session. Summing them is worst case. Hosted workspaces last until sandbox TTL. Provision for typical use and alert before the disk fills; a burst can still hit the caps. S3 moves artifact bytes off the node. |
| NIC | Same as disk. | Each TAP is capped at 50 Mbit. All guests saturating at once is unlikely. |
| Stored sessions | **Yes, by design.** | Postgres rows are not live Pi. Idle TTL frees RAM; the thread stays. Many stored sessions on one 64 GiB box is fine. Only live guests count. |

On the 64 GiB example, 48 × 512 MiB ≈ 24 GiB guests on about 56 GiB
usable is not RAM overprovision. Packing toward 110 live would leave
no headroom.

## Logs

Ship stderr. There is no log file shipper in the gateway. `apipi serve`
writes one JSON object per line. Default level is `info`. Use
`APIPI_LOG_FORMAT=text` only on a laptop.

Info covers process start (version, bind, run mode, store), one line
per HTTP request except `/health` and `/metrics`, and turn completed
or cancelled. Failed turns, sandbox boot failures, unexpected
exceptions, and HTTP 5xx are `error`. Warnings are degraded-but-running
(SQLite one-process, `run_mode=none`, capacity, lease expiry, export
drop). Debug is optional diagnosis.

Error and warning lines that operators should alert on include
`event` and `error_code`. Same id fields as traces when known
(`request_id`, `session_id`, `turn_id`, `tenant_id`, `worker_id`). The
event table is in [usage](usage.md#logs). Keep secrets and prompt
bodies out of the logs. How to scrape metrics, ship logs, and point
OTLP at a collector is in [observability](observability.md).

## Tuning

Change a setting and restart the process. There is no plan or SKU
field. Details and defaults are in [configuration](config.md).

| Setting | Why it matters |
| --- | --- |
| `APIPI_RUN_MODE` | Set `microvm` for Firecracker production isolation. Nested TOML is `[sandbox].backend`. See [configuration](config.md#sandbox). |
| `APIPI_MAX_SESSIONS` | Live Pi on this node. Hard cap (`429` `capacity`). |
| `APIPI_MAX_SESSIONS_PER_TENANT` | Live Pi for one tenant (`429` `capacity_tenant`). |
| `APIPI_MICROVM_MEM_MIB` / `APIPI_MICROVM_VCPUS` / `APIPI_SANDBOX_L_VCPUS` | Guest RAM and vCPUs. Raise RAM for the browser image. `S` and non-browser `M` stay at 1 vCPU. `L` defaults to 2. Browser guests get at least 2 even on `M`. |
| `APIPI_IDLE_TTL` | Kill idle Pi for `none` (default 15 minutes) and free a live slot. Hosted computers use sandbox TTL. |
| `APIPI_SANDBOX_TTL_OPENAI_HOSTED` | Stop hosted Pi and delete the workspace (default 1 hour). |
| `APIPI_TURN_TIMEOUT` | Cancel a stuck turn (default 10 minutes). |
| `APIPI_DB_POOL_SIZE` | Postgres connections from this process (default 5). |
| `APIPI_MAX_REQUEST_BYTES` | HTTP body cap (`413` `payload_too_large`). |
| `APIPI_MAX_WORKSPACE_BYTES` / `APIPI_MAX_ARTIFACT_BYTES` | Directory and published-artifact caps. |
| `APIPI_ARTIFACT_STORE` | `local` or `s3`. Production split mode uses `s3` so workers hold no store credentials. `local` needs one shared `APIPI_LOCAL_STORE_DIR` on the API and every worker. |
| `APIPI_MICROVM_EGRESS_ALLOWLIST` / `HOSTS` / `MBIT` | Optional destination allowlist (off by default) and 50 Mbit TAP rate. Private IPv4 ranges are always rejected. |
| `APIPI_INSTANCE_ID` | Sets `X-ApiPi-Instance` so you can confirm stickiness. |
| `APIPI_VAULT_MASTER_KEY` | Encrypts MCP vault tokens at rest. Put a 32-byte key in the process environment or a k8s secret. Unset uses a local default and logs a warning; do not leave that in production. Same key on every API process that writes or injects vault secrets. |

## Worker token rotation

Each worker has its own token, and only the hash is stored. The
secret is shown once by `apipi workers token create` and never again.
To rotate without downtime, create a second token for the worker,
roll the new secret out to `APIPI_WORKER_TOKEN_FILE`, then revoke the
old token with `apipi workers token revoke`. Several active tokens per
worker are normal. A revoked token closes live sockets on the next
heartbeat and is rejected on reconnect. See [workers](workers.md).

## Worker transport security

Worker tokens are bearer secrets, so the worker socket needs TLS
outside local development. Point workers at an `https://`
`APIPI_API_URL`: `apipi worker` fails at startup when the URL is a
non-loopback plain `http://` or `ws://` address. Loopback `http://`
URLs stay allowed for local development only.

For mutual TLS, provision one client certificate and key per worker
host and verify them on the reverse proxy or load balancer in front
of the API:

```
APIPI_WORKER_CLIENT_CERT=/run/apipi/worker.crt
APIPI_WORKER_CLIENT_KEY=/run/apipi/worker.key
APIPI_WORKER_SERVER_CA=/run/apipi/api-ca.crt
```

`APIPI_WORKER_SERVER_CA` is the optional private CA bundle the
worker uses to verify the API server certificate when the system
trust store does not cover it. The certificate and key must be set
together. On the proxy side, terminate TLS, require a client
certificate for `/internal/worker`, and verify it against your
worker CA; the API itself keeps authenticating the per-worker bearer
token on that route. Rotate client certificates like worker tokens:
roll the new pair out, then remove the old CA entry.

## Tenant-aware deployments

Most tenants share one gateway pool. Some tenants get their own pool:
a separate Host name or load-balancer backend group. Auth still
returns `tenant_id`. You map that tenant to the pool outside the
gateway. The public Agents API does not change.

Isolated in a dedicated pool: Pi and per-worker `APIPI_SESSIONS_DIR` workspaces. Shared
across pools: Postgres, and artifact, file, skill, and Pi session bytes through the
configured store (`s3` or one shared `APIPI_LOCAL_STORE_DIR`).
Sticky rules still apply inside the pool. See
[tenant pools](scale.md#tenant-pools).

## Failure and drain

Health checks should call `GET /health`. Probe health rather than a
session. That endpoint accepts requests without a bearer.

To drain a worker, `systemctl stop` (or `restart`) it. SIGTERM sets
heartbeat `"drain": true`, idle Pi exit, in-flight turns finish, then
the process exits 0. Use the drain drop-in so `TimeoutStopSec` is
longer than `--drain-timeout`. A timeout exits 1; systemd then SIGKILLs
the cgroup (`KillMode=control-group`). Keep API health successful while
a turn is in flight. A live session stays on the node that owns it.

Host workers (`none`) stamp Pi and host MCP with
`APIPI_WORKER_PID`. After a crash, the next `apipi worker` or combined
`apipi serve` start reaps processes whose stamped parent is dead. It
does not kill another live worker's Pi, and it does not match on the
`pi` command name. systemd units must set `KillMode=control-group` so
`systemctl stop` kills the unit cgroup, including Pi. A raw
`kill -9` of the worker PID does not; the startup sweep covers that.

Checklist after `kill -9` of a host worker: start the worker again,
then confirm no leftover processes remain whose `APIPI_WORKER_PID`
is the old worker PID. The new process has a new `boot_id` and does
not emit stops for sessions it did not start. A lifecycle consumer
closes the old `boot_id` when heartbeats stop, or when the same
`worker_id` or `instance_id` appears on a new `boot_id`. See
[session lifecycle export](usage.md#session-lifecycle-export).

Run NTP on workers if you export lifecycle events. `live_ms` does not
depend on the wall clock, but `ts` does, and consumers order across
hosts by that timestamp only as an approximation. Size
`APIPI_LIFECYCLE_QUEUE` for the exporter outage you can lose. The
default is 10000 events. A full queue drops the newest event and
increments `apipi_lifecycle_export_total{result="overflow"}`.

If SSE drops, reconnect with `after_seq` to replay from the store. The
next turn still needs the node that holds Pi.

A full node returns `429` with code `capacity`. A tenant at its cap
returns `429` with code `capacity_tenant`. Clients should retry later;
idle reap on the process that holds Pi frees a slot (the worker, when
the API is `--api-only`). Request bodies over `APIPI_MAX_REQUEST_BYTES`
return `413`.
