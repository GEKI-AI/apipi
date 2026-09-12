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

`apipi serve` starts the gateway. Only run mode `host` is implemented:
Pi is a child of the gateway. `jail` and `microvm` are names in config
and in this spec. If you set either of them, the process exits with
`APIPI_RUN_MODE=… is not available`. There is no fallback.

The configured default is still `jail`. Serve with
`APIPI_RUN_MODE=host` until jail exists. `host` logs a warning and is
not suited for production.

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
| `jail` | Linux namespaces. Shared kernel. Config default. | Not implemented. Process exits. |
| `microvm` | KVM guest. Own kernel. | Not implemented. Process exits. |

`host` works everywhere we run tests. `jail` is planned for Linux
(bubblewrap). `microvm` is planned for Linux with `/dev/kvm`.

### Default (one server)

The intended same-server default is Pi in `jail` with files in a
session directory next to Pi. That directory is
`environment.openai_hosted` (not OpenAI's cloud). Until jail exists,
the practical default is `APIPI_RUN_MODE=host` with the same local
directory.

`self_hosted` if the computer is elsewhere. That combines with any run
mode.

### `jail` (not available)

The plan is bubblewrap + cgroup v2 + pasta. Pi, stdio MCP, and local
file tools would run inside. No host loopback (Postgres). Not a VM.
This mode would **not** protect the host from a hostile user.

Chromium in that jail would need `--no-sandbox`. Setting
`APIPI_RUN_MODE=jail` exits today.

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
