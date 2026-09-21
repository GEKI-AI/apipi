# Isolation

Isolation is where Pi (and stdio MCP) run. It is server config
(`APIPI_RUN_MODE`), not an OpenAI field. It is independent of
[environment](environments.md), which is where file and shell tools
run. A remote `self_hosted` computer does not replace Pi isolation.
The HTTP API never runs inside a guest.

This page explains why the modes exist and what a session looks like
inside a microVM. Packages, image paths, and TAP flags are in
[run modes](run-modes.md). How API processes and workers split is in
[workers](worker-concepts.md).

## Why it matters

Pi can call a shell, write files, and load MCP servers that you did
not write. In production those sessions are untrusted relative to the
gateway and to each other. A shared-kernel container is not enough for
that: a kernel bug is shared. Hardware virt gives each session its own
kernel.

| Mode | What it is | When |
| --- | --- | --- |
| `none` | Pi is a child of the gateway process, in its own process group. | Laptops and CI. Logs a warning. Not for production. |
| `chat` | Same host backend as `none`, labeled `chat`. Teardown kills the Pi process group. Crash restart reaps leftovers. | Dedicated chat worker pools. No production warning. |
| `microvm` | One [Firecracker](https://firecracker-microvm.github.io/) KVM guest per session. | Production when a computer is in use. |
| `package.mod:Class` | An operator class behind the same isolation interface. | You already have a sandbox. |

The process default is `none` so `apipi serve` can start without KVM.
Production sets `APIPI_RUN_MODE=microvm` on the process that owns
guests: `apipi worker`, or combined `apipi serve` on a single host.
`apipi serve --api-only` does not probe KVM and does not create TAP
devices.

If the selected mode cannot start, that process exits. It does not
fall back to `none`. `host` and `jail` are not valid.

## What lives in the guest

In `microvm`, one guest holds Pi, stdio MCP, and the local computer
(`environment.openai_hosted` or the `hosted` alias). They share
`/workspace`. The gateway, Postgres, and tenant secrets stay on the
host. The model key and HTTP MCP bearers are injected by a per-session
credential broker on the TAP host IP (or loopback in `none`). Guest
`.apipi/env` does not contain those values.

```
  API / worker process          never enters the guest
           |
           |  vsock (Pi RPC)
           |  TAP (model URL, HTTP MCP)
           v
     Firecracker guest
        Pi --mode rpc
        stdio MCP
        /workspace     <- openai_hosted files
```

RPC is JSON lines over vsock. The host does not pipe stdin into the
guest process tree. Guest localhost works (loopback inside the guest).
The guest cannot use **host** loopback, so it cannot open Postgres on
the worker's `localhost`. Model calls and HTTP MCP from Pi go to the
host broker on the TAP gateway address. The broker forwards to the
real model host and MCP servers. By default the TAP may also reach
the public internet. It
is rate-limited with `tc`. An optional destination allowlist can lock
the guest to named hosts; the model host is always included. Session
`environment.network` can disable or restrict that TAP further. It
cannot open hosts the process-wide allowlist forbids. See
[configuration](config.md#networking) and
[environments](environments.md).

Jailer is useful when present. It is not required. ApiPi starts
Firecracker itself. Managers such as Flintlock remain a possible later
option if they prove a clear win; they are not the path we ship. See
[ADR 0010](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0010-apipi-firecracker.md).

## A hosted workspace

On create, the host folder is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. At guest boot that
folder is packed into a workspace drive and unpacked onto a tmpfs at
`/workspace`, which is the guest cwd. Skill paths are rewritten to
`/workspace` so Pi inside the guest can load them.

Scratch files do not survive sandbox stop. After
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with no activity, Pi
stops and the directory is deleted. The session row, the event log, and
published artifacts stay. The next turn creates an empty `/workspace`
and re-applies skills, packages, setup commands, and network policy.

Ask the agent to write under `outputs/` if you need the file after
that. That folder is harvested onto the host artifact store when a
turn completes.

## Probe

`microvm` and a custom backend that sets `needs_probe` boot a
throwaway guest and tear it down before the process accepts work. That
needs `/dev/kvm`, `firecracker`, `jailer` on `PATH` when you use it,
kernel and rootfs images, and host net tools (`ip`, `iptables`, `tc`).
`apipi install --microvm` can fetch Firecracker and build images.

`apipi worker` always probes. Combined `apipi serve` probes. `apipi
serve --api-only` skips the probe so a rootless API container can
start.

## Examples

Laptop, no KVM:

```
APIPI_RUN_MODE=none apipi serve
```

Pi is a child of that process and starts in its own process group so
teardown can kill Pi and the MCP children it started. Use a local
`openai_hosted` directory the same way. This is the process default.

One box with KVM (embedded worker):

```
APIPI_RUN_MODE=microvm apipi serve
```

That process probes Firecracker, then serves HTTP and boots guests.

API in Docker, guests on a KVM host:

```
apipi serve --api-only
APIPI_RUN_MODE=microvm apipi worker
```

The API never opens `/dev/kvm`. The worker does. See
[workers](worker-concepts.md) and [install](install.md).

`environment.type=none` means “no files.” Isolation `none` means “no
sandbox for Pi.” They are not the same setting. Worker placement for
`environment.type=none` is `APIPI_ENV_NONE_PLACEMENT` (default `chat`).
See [workers](workers.md).
