# How it works

```
  OpenAI SDK / your app
                 |
                 |  bearer key
                 v
            FastAPI gateway          <- never in the guest
            store (SQLite or Postgres)
                  |
                  |  rpc
                  v
            Pi  (+ stdio MCP)        <- none | microvm
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
gateway stays on the host.

## Status

`apipi serve` starts the gateway. Built-in run modes are `none` and
`microvm`. A custom backend is an import path. If the selected mode
cannot start, the process exits. `host` and `jail` are not valid.

Production uses `microvm`: [Firecracker](https://firecracker-microvm.github.io/)
gives each session its own kernel. The process default is `none` so a
machine without KVM can start; it logs a warning.

`microvm` starts Pi (and stdio MCP) in a Firecracker guest when
`/dev/kvm`, `firecracker`, `jailer`, the kernel and rootfs images, and
host net tools (`ip`, `iptables`, `tc`) are present, and after a throwaway
guest has booted. If any of those are missing or the probe fails, the
process exits. It does not fall back to `none`.

## Gateway

The gateway is Python 3.13 and FastAPI. It authenticates callers,
owns sessions, appends the event log, and streams SSE. It talks to
the durable store (SQLite for one process, Postgres when the store is
shared). It spawns
Pi (`pi --mode rpc`).

Pi's environment gets `OPENAI_BASE_URL`, the model key (the request
bearer, or `OPENAI_API_KEY_OVERWRITE` when set), and that session's MCP
secrets. It does not get `DATABASE_URL` or gateway keys. Pi is started
with the request `agent.model` against that host. It does not use Pi's
built-in default model. When `agent.instructions` are set, they are
appended to Pi's system prompt. Empty or omitted instructions leave
that default prompt unchanged.

Auth is a callback on the bearer. Default hashes the key. We do not
store secrets. See [auth](auth.md).

## Run mode

Run mode is server config, not an OpenAI field. Set `APIPI_RUN_MODE`.
If the mode cannot start, the process exits. Operator install,
systemd, Docker, and storage are in [run modes](run-modes.md).

| Mode | Isolation | Today |
| --- | --- | --- |
| `none` | None. Pi is a child of the gateway. | Implemented. Logs a warning. Not for production. |
| `microvm` | KVM guest. Own kernel. Production when a computer is in use. | Implemented when `/dev/kvm`, `firecracker`, `jailer`, guest images, `ip`, `iptables`, and `tc` can start, and a throwaway guest boots. Otherwise the process exits. |
| `package.mod:Class` | Operator-provided backend. | Loaded at startup. Probe runs when the backend sets `needs_probe`. |

Run production under systemd on the host. The Compose file starts
Postgres only. Host sizing and scale-out are in [production](production.md).
Several `apipi serve` processes need sticky routing because Pi
and local files live on one node. See [multiple nodes](scale.md).

### Default (one server)

The intended same-server production path is Pi in `microvm` with files
in a session directory next to Pi. That directory is
`environment.openai_hosted` (or the `hosted` alias). It is not
OpenAI's cloud. Pi and those files share one guest. Operators without
KVM should set `APIPI_RUN_MODE=none` with the same local directory.

`self_hosted` if the computer is elsewhere. That is the only supported
split. It combines with any run mode. The customer must sandbox the
runner.

### `microvm`

[Firecracker](https://firecracker-microvm.github.io/) gives each
session a KVM guest with its own kernel. Shared-kernel jails are not
enough for untrusted multi-tenant computers; hardware virt is. The
gateway never enters the guest. Pi, stdio MCP, and local file tools
boot inside it. The session directory
(`environment.openai_hosted`) is packed into a workspace drive at
boot, unpacked onto a guest tmpfs at `/workspace`, and is the guest
cwd. Scratch files do not survive sandbox stop. Skill directories from
that workspace are packed with it, and `--skill` paths are rewritten
to `/workspace` so Pi inside the guest can load them.

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

This is the mode that protects the host from a hostile session. The
guest rootfs is operator-provided. It should include Node, Pi, and
`/sbin/apipi-guest` (the script shipped as `src/apipi/pi/guest.sh`).
That init mounts a tmpfs workspace, unpacks the workspace drive,
brings up the TAP interface, and bridges vsock port 52 to
`pi --mode rpc`. The guest needs `python3` or `socat` for that
bridge. Build a rootfs with `scripts/microvm-rootfs`. Set
`APIPI_MICROVM_KERNEL` and `APIPI_MICROVM_ROOTFS` to the image files.
Missing paths are a configuration error.

Chromium can use its own sandbox inside the guest, which is where
browsers belong in this stack.

VMM overhead is small (~5 MiB). Real cost is guest RAM (Pi alone is
modest; Playwright needs hundreds of MiB).

If `/dev/kvm`, `firecracker`, `jailer`, the kernel file, the rootfs
file, `ip`, `iptables`, or `tc` cannot start, or the throwaway guest
probe fails, `apipi serve` exits before it binds HTTP. There is no
fallback to `none`.

## Environment

See [environments](environments.md).

| `environment.type` | File/shell tools |
| --- | --- |
| `openai_hosted` (default) | Session directory, same place as Pi. `hosted` is an alias. |
| `none` | No file tools |
| `self_hosted` | Proxied to a runner. Outside the guest. The only supported split. |

## Store

The durable store holds tenants, agents, sessions, turns, items, the event
log, hot usage (turn log and/or daily rollups, never prompt text),
environment state, and artifact metadata. Artifact bytes sit in the
configured artifact store after a turn completes: local files by
default, or S3-compatible object storage. Not API keys. Long-term
usage analytics use the optional HTTPS export. Prometheus and
OpenTelemetry are exports. See [usage](usage.md) and
[run modes](run-modes.md#storage).

Pi JSONL is a cache. Do not read it to serve the API. The
`openai_hosted` workspace is ephemeral: after
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with no activity,
Pi stops and the directory is deleted, or the session is deleted.

One Pi process or guest per session. Hosted computers use the sandbox
TTL. `none` and `self_hosted` sessions use `APIPI_IDLE_TTL` (default
15 minutes) to free RAM. Files under `artifacts/` and `outputs/` are
copied to the host store when a turn completes. The session row
stays. Resume from the event log; hosted scratch files do not. Live
processes are capped by `APIPI_MAX_SESSIONS` and
`APIPI_MAX_SESSIONS_PER_TENANT`. Workspace and artifact bytes are
capped per session. See [config](config.md).

Cross-tenant IDs return `404`, not `403`. Live Pi and the local
workspace stay on the node that created the session. Published
artifact bytes follow `APIPI_ARTIFACT_STORE`. Postgres is shared
across nodes. SQLite is one process only.


## Tools

Function tools, MCP, and skills. See [tools](tools.md).
