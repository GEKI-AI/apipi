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

`apipi serve` starts the gateway. Run modes `host` and `jail` are
implemented. `microvm` is a name in config and in this spec. If you
set `microvm`, the process exits with `APIPI_RUN_MODE=microvm is not
available`. There is no fallback.

`jail` is the configured default. It starts Pi (and stdio MCP) in a
Linux namespace jail when `bwrap`, `pasta`, and cgroup v2 can start.
If those tools are missing, the process exits. It does not fall back
to `host`. Operators without jail tools must set
`APIPI_RUN_MODE=host`. `host` logs a warning and is not suited for
production.

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
| `microvm` | KVM guest. Own kernel. | Not implemented. Process exits. |

`host` works everywhere we run tests. `jail` needs Linux with
bubblewrap, pasta, and cgroup v2. `microvm` is planned for Linux with
`/dev/kvm`.

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

### `microvm` (not available)

The plan is [Firecracker](https://firecracker-microvm.github.io/) +
jailer. Pi (and stdio MCP) would boot in a guest with its own kernel.
The session directory would be the guest workspace. RPC would go over
vsock.

This is the mode that would protect the host from users. The guest
would have a minimal rootfs (Node, Pi). Chromium could use its own
sandbox inside the guest.

VMM overhead is small (~5 MiB). Real cost is guest RAM (Pi alone is
modest; Playwright needs hundreds of MiB). Setting
`APIPI_RUN_MODE=microvm` exits today.

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

One Pi process (or, later, guest) per session. After 15 minutes idle
(`APIPI_IDLE_TTL`), kill the process. The session row stays. Resume
from the event log.

Cross-tenant IDs return `404`, not `403`.

## Tools

Function tools, MCP, and skills. See [tools](tools.md).
