# Architecture

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

`jail` is the configured default. It starts Pi (and stdio MCP) in a
Linux namespace jail when `bwrap`, `pasta`, and cgroup v2 can start.
If those tools are missing, the process exits. It does not fall back
to `host`. Operators without jail tools must set
`APIPI_RUN_MODE=host`. `host` logs a warning and is not suited for
production.

`microvm` starts Pi (and stdio MCP) in a Firecracker guest when
`/dev/kvm`, `firecracker`, `jailer`, the kernel and rootfs images, and
host net tools (`ip`, `iptables`) are present. If any of those are
missing, the process exits. It does not fall back to `jail` or `host`.

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
If the mode cannot start, the process exits.

| Mode | Isolation | Today |
| --- | --- | --- |
| `host` | None. Pi is a child of the gateway. | Implemented. Logs a warning. |
| `jail` | Linux namespaces. Shared kernel. Config default. | Implemented when `bwrap`, `pasta`, and cgroup v2 can start. Otherwise the process exits. |
| `microvm` | KVM guest. Own kernel. | Implemented when `/dev/kvm`, `firecracker`, `jailer`, guest images, `ip`, and `iptables` can start. Otherwise the process exits. |

`host` works everywhere we run tests. `jail` needs Linux with
bubblewrap, pasta, and cgroup v2. `microvm` needs Linux with
`/dev/kvm`, Firecracker, jailer, operator-provided kernel and rootfs
images, `ip`, and `iptables`.

### Default (one server)

The intended same-server default is Pi in `jail` with files in a
session directory next to Pi. That directory is
`environment.openai_hosted` (not OpenAI's cloud). Operators without
jail tools should set `APIPI_RUN_MODE=host` with the same local
directory.

`self_hosted` if the computer is elsewhere. That combines with any run
mode.

### `jail`

bubblewrap + cgroup v2 + pasta. The gateway never enters the jail. Pi,
stdio MCP, and local file tools run inside. The session directory
(`environment.openai_hosted`) is bind-mounted into the jail and is the
cwd.

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
boot, unpacked onto a guest tmpfs, and is the guest cwd. Writes stay
in the guest. They are not copied back to the host folder. That
differs from jail, which bind-mounts the same directory.
Skill directories from that workspace are packed with it, and
`--skill` paths are rewritten to `/tmp/workspace` so Pi inside the
guest can load them.

RPC is JSON lines over vsock. The gateway does not pipe host stdin
into the guest process tree. The Pi adapter still sends the same
JSON-line RPC on that vsock stream.

There is no host loopback, so the guest cannot reach Postgres on
localhost. Network for the model URL and HTTP MCP goes through a TAP
device and NAT, not host loopback. Pi RPC still runs over vsock.

This is the mode that protects the host from a hostile user. The
guest rootfs is operator-provided. It should include Node, Pi, and
`/sbin/apipi-guest` (the script shipped as `src/apipi/pi/guest.sh`).
That init mounts a tmpfs workspace, unpacks the workspace drive,
brings up the TAP interface, and bridges vsock port 52 to
`pi --mode rpc`. The guest needs `python3` or `socat` for that
bridge. Do not vendor a distro in git. Set `APIPI_MICROVM_KERNEL` and
`APIPI_MICROVM_ROOTFS` to the image files. Missing paths are a
configuration error.

Chromium can use its own sandbox inside the guest. Do not put
Chromium in the gateway.

VMM overhead is small (~5 MiB). Real cost is guest RAM (Pi alone is
modest; Playwright needs hundreds of MiB).

If `/dev/kvm`, `firecracker`, `jailer`, the kernel file, the rootfs
file, `ip`, or `iptables` cannot start, `apipi serve` exits. There is
no fallback to `jail` or `host`.

## Environment

See [environments](environments.md).

| `environment.type` | File/shell tools |
| --- | --- |
| `openai_hosted` (default) | Session directory, same place as Pi |
| `none` | No file tools |
| `self_hosted` | Proxied to a runner. Outside jail/guest. |

## Store

Postgres holds tenants, agents, sessions, turns, items, the event
log, the turn log (usage tokens and details, never prompt text),
environment state, and artifact metadata. Not API keys. Prometheus and
OpenTelemetry are exports. See [usage](usage.md).

Pi JSONL is a cache. Do not read it to serve the API.

One Pi process or guest per session. After 15 minutes idle
(`APIPI_IDLE_TTL`), kill the process. The session row stays. Resume
from the event log.

Cross-tenant IDs return `404`, not `403`.

## Tools

Function tools, MCP, and skills. See [tools](tools.md).
