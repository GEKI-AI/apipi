# Production

How to choose hosts, scale out, and size an ApiPi process. Production
is `apipi serve --api-only` plus `apipi worker` on KVM. Combined
`apipi serve` is one box. Why that split exists is in
[workers](worker-concepts.md). Guest internals are in
[isolation](isolation.md).

## Host selection

Run production as `apipi serve --api-only` plus `apipi worker` on
KVM hosts with `APIPI_RUN_MODE=microvm`. Combined `apipi serve` is the
single-host embedded worker. Nested Docker or nested KVM is a lab
setup. The Compose file in this repo starts Postgres (and can run a
rootless API). Drain a worker with a heartbeat `"drain": true` before
you stop the unit so new leases go elsewhere. Expired leases fail
closed; they are not reassigned.

Isolation `none` is for local machines and CI. If the selected mode
cannot start, `apipi serve` exits before it binds HTTP. The process
never switches to another mode on its own.

Size the box from **live** sessions, not from Postgres row counts.
Each live session is one Pi process or Firecracker guest. Guest RAM is
the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Firecracker VMM
overhead is small (~5 MiB per guest). Pi alone is modest. Playwright
or Chromium inside the guest needs hundreds of MiB extra, so raise
guest RAM rather than packing more 512 MiB guests.

Leave disk for `APIPI_SESSIONS_DIR`: each `openai_hosted` directory is
capped at `APIPI_MAX_WORKSPACE_BYTES` (default 1 GiB) and lasts until
sandbox TTL. Local artifact
bytes add up to `APIPI_MAX_ARTIFACT_BYTES` (default 512 MiB) per
session unless you set `APIPI_ARTIFACT_STORE=s3`.

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

## Scale-out

| Shape | When | What stays on the node | What is shared |
| --- | --- | --- | --- |
| Combined, one host | You fit in `max_sessions` on one box | Pi, SSE, WebSockets, `openai_hosted` directories, local artifacts | SQLite or Postgres |
| API-only + workers | Production. API in Docker or several replicas | Guests and workspaces on **workers**. API is stateless for Pi | Postgres, worker token. Artifact bytes too when `APIPI_ARTIFACT_STORE=s3` |
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
kills Pi and frees that RAM. The session row can outlive the process.
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
| Playwright / Chromium | Use sandbox size **`L`** so the guest gets the **browser** rootfs, about **2 GiB** RAM, and Playwright MCP against system Chromium. Then about **24–28** live `L` guests in a 56 GiB budget. `S` (512 MiB) is for Pi and light tools. |
| CPU | 48 × 1 vCPU on 12 cores is normal while turns wait on the model URL. Keep `APIPI_MICROVM_VCPUS=1` unless the computer is CPU-heavy. |
| Disk | Hosted workspaces last until sandbox TTL (default 1 hour), capped at 1 GiB each. Local artifacts 512 MiB per session unless S3. Worst case is cap × live-and-idle directories, not typical use. |
| NIC | Each guest TAP is 50 Mbit. 48 guests all saturated ≈ 2.4 Gbit. That is the ceiling, not the plan. |

On a shared node set `APIPI_MAX_SESSIONS_PER_TENANT` lower than the
node cap (for example 8). A tenant that would pass it gets `429` with
code `capacity_tenant`.

`self_hosted` still costs a Pi guest on this host. The runner disk is
elsewhere and is not capped here.

## Overprovision

| Resource | Overprovision? | Why |
| --- | --- | --- |
| Guest RAM | **No.** Set `worker_memory_mb` so the sum of guest `mem_mib` plus the host reserve fits. Size `max_sessions` as a second hard cap. | Each live session is a Firecracker guest with that RAM. There is no balloon device. The guest kernel usually touches the memory. The scheduler will not oversubscribe RAM or session count. |
| CPU | **Yes.** Default 1 vCPU per guest. | Turns mostly wait on the model URL. Watch host load, not the vCPU count. Raise `microvm_vcpus` only if the computer is CPU-heavy (builds, Playwright). |
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
or failed. Failed turns and unexpected exceptions are `error`.
Warnings are degraded-but-running (SQLite one-process, `run_mode=none`,
export drop). Debug is optional diagnosis.

Same id fields as traces when known (`request_id`, `session_id`,
`turn_id`). Keep secrets and prompt bodies out of the logs.

## Tuning

Change a setting and restart the process. There is no plan or SKU
field. Details and defaults are in [configuration](config.md).

| Setting | Why it matters |
| --- | --- |
| `APIPI_RUN_MODE` | Set `microvm` for Firecracker production isolation. Nested TOML is `[sandbox].backend`. See [configuration](config.md#sandbox). |
| `APIPI_MAX_SESSIONS` | Live Pi on this node. Hard cap (`429` `capacity`). |
| `APIPI_MAX_SESSIONS_PER_TENANT` | Live Pi for one tenant (`429` `capacity_tenant`). |
| `APIPI_MICROVM_MEM_MIB` / `APIPI_MICROVM_VCPUS` | Guest RAM and vCPUs. Raise RAM for Playwright. Keep 1 vCPU unless the computer is CPU-heavy. |
| `APIPI_IDLE_TTL` | Kill idle Pi for `none` and `self_hosted` (default 15 minutes) and free a live slot. Hosted computers use sandbox TTL. |
| `APIPI_SANDBOX_TTL_OPENAI_HOSTED` | Stop hosted Pi and delete the workspace (default 1 hour). |
| `APIPI_TURN_TIMEOUT` | Cancel a stuck turn (default 10 minutes). |
| `APIPI_DB_POOL_SIZE` | Postgres connections from this process (default 5). |
| `APIPI_MAX_REQUEST_BYTES` | HTTP body cap (`413` `payload_too_large`). |
| `APIPI_MAX_WORKSPACE_BYTES` / `APIPI_MAX_ARTIFACT_BYTES` | Directory and published-artifact caps. |
| `APIPI_ARTIFACT_STORE` | `local` or `s3`. Use `s3` when more than one node serves artifact, hosted file, or skill bytes. |
| `APIPI_MICROVM_EGRESS_ALLOWLIST` / `HOSTS` / `MBIT` | Optional destination allowlist (off by default) and 50 Mbit TAP rate. |
| `APIPI_INSTANCE_ID` | Sets `X-ApiPi-Instance` so you can confirm stickiness. |

## Tenant-aware deployments

Most tenants share one gateway pool. Some tenants get their own pool:
a separate Host name or load-balancer backend group. Auth still
returns `tenant_id`. You map that tenant to the pool outside the
gateway. The public Agents API does not change.

Isolated in a dedicated pool: Pi and `APIPI_SESSIONS_DIR`. Shared
across pools: Postgres, and object-store bytes (artifacts, hosted files,
skills) when the store is `s3`.
Sticky rules still apply inside the pool. See
[tenant pools](scale.md#tenant-pools).

## Failure and drain

Health checks should call `GET /health`. Probe health rather than a
session. That endpoint accepts requests without a bearer.

To drain a node, take it out of the upstream, wait until in-flight
turns finish or idle TTL has killed Pi, then stop the systemd unit.
Keep health successful while a turn is in flight. A live session stays
on the node that owns it.

If SSE drops, reconnect with `after_seq` to replay from the store. The
next turn still needs the node that holds Pi.

A full node returns `429` with code `capacity`. A tenant at its cap
returns `429` with code `capacity_tenant`. Clients should retry later;
idle reap frees a slot. Request bodies over `APIPI_MAX_REQUEST_BYTES`
return `413`.
