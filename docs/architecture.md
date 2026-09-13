# How it works

```
  OpenAI SDK / your app
                 |
                 |  bearer key
                 v
           FastAPI gateway          <- never in jail or guest
           Postgres
                 |
                 |  rpc
                 v
           Pi  (+ stdio MCP)        <- host | jail | microvm
                 |
                 +-- local files    <- next to Pi
                 +-- or remote env  <- self_hosted runner
                 +-- HTTP MCP       <- e.g. Tavily
```

Two independent choices:

| | What it controls |
| --- | --- |
| **Run mode** | Where Pi (and stdio MCP) run |
| **Environment** | Where file/shell tools run |

They combine. A remote environment does not replace Pi isolation. The
gateway always stays on the host. It is never placed inside a jail or
a guest.

## Status

`apipi serve` starts the gateway. Run modes `host`, `jail`, and
`microvm` are implemented. There is no fallback from one mode to
another.

Production SaaS and enterprise use `microvm`. The process default is
`jail` so a machine without KVM can still start; that is a fallback,
not the production posture. `host` logs a warning and is not suited
for production.

`jail` starts Pi (and stdio MCP) in a Linux namespace jail when
`bwrap`, `pasta`, and cgroup v2 can start, and after a throwaway jail
has launched. If that probe fails, the process exits before it binds
HTTP. It does not fall back to `host`. Operators without jail tools
must set `APIPI_RUN_MODE=host`.

`microvm` starts Pi (and stdio MCP) in a Firecracker guest when
`/dev/kvm`, `firecracker`, `jailer`, the kernel and rootfs images, and
host net tools (`ip`, `iptables`, `tc`) are present, and after a throwaway
guest has booted. If any of those are missing or the probe fails, the
process exits. It does not fall back to `jail` or `host`.

## Gateway

The gateway is Python 3.13 and FastAPI. It authenticates callers,
owns sessions, appends the event log, and streams SSE. It talks to
Postgres. It spawns Pi (`pi --mode rpc`).

Pi's environment gets model credentials (`OPENAI_BASE_URL`,
`OPENAI_API_KEY`) and that session's MCP secrets. It does not get
`DATABASE_URL` or gateway keys.

Auth is a callback on the bearer. Default hashes the key. We do not
store secrets. See [auth](auth.md).

## Run mode

Run mode is server config, not an OpenAI field. Set `APIPI_RUN_MODE`.
If the mode cannot start, the process exits. Operator install,
systemd, Docker, and storage are in [run modes](run-modes.md).

| Mode | Isolation | Today |
| --- | --- | --- |
| `host` | None. Pi is a child of the gateway. | Implemented. Logs a warning. Not for production. |
| `jail` | Linux namespaces. Shared kernel. Fallback when microvm cannot run. | Implemented when `bwrap`, `pasta`, and cgroup v2 can start, and a throwaway jail launches. Otherwise the process exits. |
| `microvm` | KVM guest. Own kernel. Production when a computer is in use. | Implemented when `/dev/kvm`, `firecracker`, `jailer`, guest images, `ip`, `iptables`, and `tc` can start, and a throwaway guest boots. Otherwise the process exits. |

Production is systemd on the host. The Compose file starts Postgres
only.

### Default (one server)

The intended same-server production path is Pi in `microvm` with files
in a session directory next to Pi. That directory is
`environment.openai_hosted` (or the `hosted` alias). It is not
OpenAI's cloud. Pi and those files share one guest. Operators without
KVM should set `APIPI_RUN_MODE=jail`. Operators without jail tools
should set `APIPI_RUN_MODE=host` with the same local directory.

`self_hosted` if the computer is elsewhere. That is the only supported
split. It combines with any run mode. The customer must sandbox the
runner.

### `jail`

bubblewrap + cgroup v2 + pasta. The gateway never enters the jail. Pi,
stdio MCP, and local file tools run inside. The session directory
(`environment.openai_hosted`) is bind-mounted into the jail and is the
cwd. The rest of `APIPI_SESSIONS_DIR` is a tmpfs, so other session
directories are not readable.

There is no host loopback, so the jail cannot reach Postgres on
localhost. Network for the model URL and HTTP MCP goes through pasta,
not `--share-net` onto the host. This is not a VM. It does **not**
protect the host from a hostile user.

Chromium in that jail needs `--no-sandbox`. If `bwrap`, `pasta`, or
cgroup v2 cannot start, `apipi serve` exits. There is no fallback to
`host`.

### `microvm`

[Firecracker](https://firecracker-microvm.github.io/) + jailer. The
gateway never enters the guest. Pi, stdio MCP, and local file tools
boot in a KVM guest with its own kernel. The session directory
(`environment.openai_hosted`) is packed into a workspace drive at
boot, unpacked onto a guest tmpfs, and is the guest cwd. Before the
guest exits, those writes are pulled back to the host folder.
Skill directories from that workspace are packed with it, and
`--skill` paths are rewritten to `/tmp/workspace` so Pi inside the
guest can load them.

RPC is JSON lines over vsock. The gateway does not pipe host stdin
into the guest process tree. The Pi adapter still sends the same
JSON-line RPC on that vsock stream.

There is no host loopback, so the guest cannot reach Postgres on
localhost. Network for the model URL and HTTP MCP goes through a TAP
device and NAT, not host loopback. Pi RPC still runs over vsock. That
TAP is allowlisted and rate-limited by default. Unlisted destinations
are rejected. The gateway's HTTP MCP probe is not TAP traffic; Pi's
calls from the guest are. See [run modes](run-modes.md) and
[config](config.md).

This is the mode that protects the host from a hostile user. The
guest rootfs is operator-provided. It should include Node, Pi, and
`/sbin/apipi-guest` (the script shipped as `src/apipi/pi/guest.sh`).
That init mounts a tmpfs workspace, unpacks the workspace drive,
brings up the TAP interface, and bridges vsock port 52 to
`pi --mode rpc`. The guest needs `python3` or `socat` for that
bridge. Do not vendor a distro in git. Build a rootfs with
`scripts/microvm-rootfs`. Set `APIPI_MICROVM_KERNEL` and
`APIPI_MICROVM_ROOTFS` to the image files. Missing paths are a
configuration error. See [run modes](run-modes.md).

Chromium can use its own sandbox inside the guest. Do not put
Chromium in the gateway.

VMM overhead is small (~5 MiB). Real cost is guest RAM (Pi alone is
modest; Playwright needs hundreds of MiB).

If `/dev/kvm`, `firecracker`, `jailer`, the kernel file, the rootfs
file, `ip`, `iptables`, or `tc` cannot start, or the throwaway guest
probe fails, `apipi serve` exits before it binds HTTP. There is no
fallback to `jail` or `host`.

## Environment

See [environments](environments.md).

| `environment.type` | File/shell tools |
| --- | --- |
| `openai_hosted` (default) | Session directory, same place as Pi. `hosted` is an alias. |
| `none` | No file tools |
| `self_hosted` | Proxied to a runner. Outside jail/guest. The only supported split. |

## Store

Postgres holds tenants, agents, sessions, turns, items, the event
log, the turn log (usage tokens and details, never prompt text),
environment state, and artifact metadata. Artifact bytes sit on the
gateway host after a turn completes. Not API keys. Prometheus and
OpenTelemetry are exports. See [usage](usage.md) and
[run modes](run-modes.md#storage).

Pi JSONL is a cache. Do not read it to serve the API. The
`openai_hosted` workspace lasts across Pi stop until
`APIPI_WORKSPACE_TTL` (default 1 hour) with no session activity, or
until the session is deleted.

One Pi process or guest per session. After the idle TTL
(`APIPI_IDLE_TTL`, default 15 minutes), kill the process. That does
not delete the workspace. Files under `artifacts/` and `outputs/` are
copied to the host store when a turn completes. The session row
stays. Resume from the event log. Live processes are capped by
`APIPI_MAX_SESSIONS` and `APIPI_MAX_SESSIONS_PER_TENANT`. Workspace
and artifact bytes are capped per session. See [config](config.md).

Cross-tenant IDs return `404`, not `403`.

## Tools

Function tools, MCP, and skills. See [tools](tools.md).
