# Production

How to choose hosts, scale out, and size an ApiPi process. The API
process runs Pi (`none` or `microvm`) plus a local directory or a
`self_hosted` runner.

## Host selection

Run production under systemd on the host with `APIPI_RUN_MODE=microvm`
so each session is a Firecracker guest with its own kernel. The host
needs `/dev/kvm` (bare metal, or a VM that exposes KVM). Nested Docker
or nested KVM is a lab setup. It is not the production path. The
Compose file in this repo starts Postgres only.

Isolation `none` is for local machines and CI. If the selected mode
cannot start, `apipi serve` exits before it binds HTTP. There is no
silent fallback.

Size the box from **live** sessions, not from Postgres row counts.
Each live session is one Pi process or Firecracker guest. Guest RAM is
the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Firecracker VMM
overhead is small (~5 MiB per guest). Pi alone is modest. Playwright
or Chromium inside the guest needs hundreds of MiB extra, so raise
guest RAM rather than packing more 512 MiB guests.

Leave disk for `APIPI_SESSIONS_DIR`: each `openai_hosted` directory is
capped at `APIPI_MAX_WORKSPACE_BYTES` (default 1 GiB) and lasts until
`APIPI_WORKSPACE_TTL` after Pi has already stopped. Local artifact
bytes add up to `APIPI_MAX_ARTIFACT_BYTES` (default 512 MiB) per
session unless you set `APIPI_ARTIFACT_STORE=s3`.

Keeping Postgres off the gateway host leaves more RAM for guests. One
`apipi serve` per host; extra uvicorn workers do not share the Pi pool.

## Scale-out

| Shape | When | What stays on the node | What is shared |
| --- | --- | --- | --- |
| One host | You fit in `max_sessions` on one box | Pi, SSE, WebSockets, `openai_hosted` directories, local artifacts | Postgres |
| Several hosts, sticky load balancer | More live sessions than one box | Same as one host, plus each process has its own `APIPI_SESSIONS_DIR` | Postgres, auth callback. Artifact bytes too when `APIPI_ARTIFACT_STORE=s3` |
| External artifact store | Clients read artifacts from any node, or you do not want artifact files on the gateway disk | Live workspace still on the node | Postgres, S3-compatible bucket |

`POST /v1/agents/sessions` may land on any node. Follow-up REST and
SSE must return to the node that owns Pi. There is no live handoff.
How to hash `session_id` and an nginx example are in
[multiple nodes](scale.md).

## Sizing

Count **live** Pi processes (or guests). Idle TTL (default 15 minutes)
kills Pi and frees that RAM. The session row and the workspace can
outlive the process. `max_sessions` does not count Postgres rows.

```
max_sessions ≈ (host RAM − reserve) / microvm_mem_mib
```

Reserve several GiB for the OS, the Python gateway, jailer, and page
cache. Colocated Postgres needs more. Do not pick `max_sessions` from
average guest RSS. Pick it from **reserved** guest RAM. `max_sessions`
is a hard cap: a new turn that would pass it returns `429` with code
`capacity`.

### Example: 64 GiB RAM, 12 cores

Postgres on another host. `APIPI_RUN_MODE=microvm`. Default guest RAM
512 MiB and 1 vCPU.

| | |
| --- | --- |
| Reserve | About **8 GiB** for OS, gateway, jailer, and page cache. |
| Default 32 live | 32 × 512 MiB ≈ **16 GiB** guests plus ~0.2 GiB VMM. Fits easily. |
| Starting cap | **`max_sessions=48`** (24 GiB guests) or keep **32**. Raise after you watch host RSS and `429` `capacity`. |
| Ceiling | (64 − 8) / 0.5 ≈ **110** live at 512 MiB. That is the wall, not a starting point. |
| Playwright / Chromium | Raise `APIPI_MICROVM_MEM_MIB` to **1024–2048**. Then about **24–48** live on this box. 512 MiB is for Pi and light tools. |
| CPU | 48 × 1 vCPU on 12 cores is normal while turns wait on the model URL. Keep `APIPI_MICROVM_VCPUS=1` unless the computer is CPU-heavy. |
| Disk | Workspaces persist after Pi stop until `workspace_ttl` (default 1 hour), capped at 1 GiB each. Local artifacts 512 MiB per session unless S3. Worst case is cap × live-and-idle directories, not typical use. |
| NIC | Each guest TAP is 50 Mbit. 48 guests all saturated ≈ 2.4 Gbit. That is the ceiling, not the plan. |

On a shared node set `APIPI_MAX_SESSIONS_PER_TENANT` lower than the
node cap (for example 8). A tenant that would pass it gets `429` with
code `capacity_tenant`.

`self_hosted` still costs a Pi guest on this host. The runner disk is
elsewhere and is not capped here.

## Overprovision

| Resource | Overprovision? | Why |
| --- | --- | --- |
| Guest RAM | **No.** Size `max_sessions` so `max_sessions × microvm_mem_mib` plus the host reserve fits. | Each live session is a Firecracker guest with that RAM. There is no balloon device. The guest kernel usually touches the memory. `max_sessions` is a hard cap, not a hint. |
| CPU | **Yes.** Default 1 vCPU per guest. | Turns mostly wait on the model URL. Watch host load, not the vCPU count. Raise `microvm_vcpus` only if the computer is CPU-heavy (builds, Playwright). |
| Disk | Caps are maxima, not reservations. | `max_workspace_bytes` and `max_artifact_bytes` are per session. Summing them is worst case. Idle workspaces last until `workspace_ttl`. Provision for typical use and alert before the disk fills; a burst can still hit the caps. S3 moves artifact bytes off the node. |
| NIC | Same as disk. | Each TAP is capped at 50 Mbit. All guests saturating at once is unlikely. |
| Stored sessions | **Yes, by design.** | Postgres rows are not live Pi. Idle TTL frees RAM; the thread stays. Many stored sessions on one 64 GiB box is fine. Only live guests count. |

On the 64 GiB example, 48 × 512 MiB ≈ 24 GiB guests on about 56 GiB
usable is not RAM overprovision. Packing toward 110 live would leave
no headroom.

## Tuning

Change a setting and restart the process. There is no plan or SKU
field. Details and defaults are in [configuration](config.md).

| Setting | Why it matters |
| --- | --- |
| `APIPI_RUN_MODE` | Set `microvm` for Firecracker production isolation. `none` is not production. Nested TOML is `[sandbox].backend`. See [configuration](config.md#sandbox). |
| `APIPI_MAX_SESSIONS` | Live Pi on this node. Hard cap (`429` `capacity`). |
| `APIPI_MAX_SESSIONS_PER_TENANT` | Live Pi for one tenant (`429` `capacity_tenant`). |
| `APIPI_MICROVM_MEM_MIB` / `APIPI_MICROVM_VCPUS` | Guest RAM and vCPUs. Raise RAM for Playwright. Keep 1 vCPU unless the computer is CPU-heavy. |
| `APIPI_IDLE_TTL` | Kill idle Pi (default 15 minutes) and free a live slot. |
| `APIPI_WORKSPACE_TTL` | Delete the local directory after Pi is already gone (default 1 hour). |
| `APIPI_TURN_TIMEOUT` | Cancel a stuck turn (default 10 minutes). |
| `APIPI_DB_POOL_SIZE` | Postgres connections from this process (default 5). |
| `APIPI_MAX_REQUEST_BYTES` | HTTP body cap (`413` `payload_too_large`). |
| `APIPI_MAX_WORKSPACE_BYTES` / `APIPI_MAX_ARTIFACT_BYTES` | Directory and published-artifact caps. |
| `APIPI_ARTIFACT_STORE` | `local` or `s3`. Use `s3` when more than one node serves artifact bytes. |
| `APIPI_MICROVM_EGRESS_ALLOWLIST` / `HOSTS` / `MBIT` | Guest TAP allowlist (on by default) and 50 Mbit rate. |
| `APIPI_INSTANCE_ID` | Sets `X-ApiPi-Instance` so you can confirm stickiness. |

## Tenant-aware deployments

Most tenants share one gateway pool. Some tenants get their own pool:
a separate Host name or load-balancer backend group. Auth still
returns `tenant_id`. You map that tenant to the pool outside the
gateway. The public Agents API does not change.

Isolated in a dedicated pool: Pi and `APIPI_SESSIONS_DIR`. Shared
across pools: Postgres, and artifact bytes when the store is `s3`.
Sticky rules still apply inside the pool. See
[tenant pools](scale.md#tenant-pools).

## Failure and drain

Health checks should call `GET /health`. Do not probe a session. That
endpoint does not require a bearer.

To drain a node, take it out of the upstream, wait until in-flight
turns finish or idle TTL has killed Pi, then stop the systemd unit.
Do not fail health in the middle of a turn. There is no live handoff
to another node.

If SSE drops, reconnect with `after_seq` to replay from Postgres. The
next turn still needs the node that holds Pi.

A full node returns `429` with code `capacity`. A tenant at its cap
returns `429` with code `capacity_tenant`. Clients should retry later;
idle reap frees a slot. Request bodies over `APIPI_MAX_REQUEST_BYTES`
return `413`.
