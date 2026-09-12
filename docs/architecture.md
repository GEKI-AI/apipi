# Architecture

```
  OpenAI SDK / your app / example UI
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

They combine. A remote environment does not replace the Pi isolation.

## Gateway

Python / FastAPI. Auth, sessions, events, SSE, webhooks. Talks to
Postgres. Spawns Pi (`pi --mode rpc`). Stays on the host.

Pi env gets model credentials and that session's MCP secrets. It does
not get `DATABASE_URL` or gateway keys.

Auth is a callback on the bearer. Default hashes the key. We do not
store secrets. See [auth](auth.md).

## Run mode

Server config. Not an OpenAI field. `APIPI_RUN_MODE`, default `jail`.
If the mode cannot start, the process exits. No fallback.

| Mode | Isolation |
| --- | --- |
| `host` | None. Pi is a child of the gateway. |
| `jail` | Linux namespaces. Shared kernel. **Default.** |
| `microvm` | KVM guest. Own kernel. |

`host` logs a warning at startup: not suited for production.

`host` works everywhere. `jail` is Linux (bubblewrap). `microvm` is
Linux and needs `/dev/kvm`.

### Default (one server)

Pi in `jail`. Files in a session directory next to Pi. No remote
runner. That directory is `environment.openai_hosted` (not OpenAI's
cloud).

`self_hosted` if the computer is elsewhere. Combines with any run mode.

### `jail`

bubblewrap + cgroup v2 + pasta. Pi, stdio MCP, and local file tools
run inside. No host loopback (Postgres). Not a VM. Does **not**
protect the host from a hostile user.

Chromium in the jail needs `--no-sandbox`.

### `microvm`

[Firecracker](https://firecracker-microvm.github.io/) + jailer.
Pi (and stdio MCP) boot in a guest with its own kernel. Session
directory is the guest workspace. RPC over vsock.

This is the mode that protects the host from users. Guest has a
minimal rootfs (Node, Pi). Chromium can use its own sandbox inside
the guest.

VMM overhead is small (~5 MiB). Real cost is guest RAM (Pi alone is
modest; Playwright needs hundreds of MiB).

## Environment

See [environments](environments.md).

| `environment.type` | File/shell tools |
| --- | --- |
| `openai_hosted` (default) | Session directory, same place as Pi |
| `none` | No file tools |
| `self_hosted` | Proxied to a runner. Outside jail/guest. |

## Store

Postgres holds tenants, agents, sessions, turns, items, the event
log, environment state, usage, artifact metadata. Not API keys.

Pi JSONL is a cache. Do not read it to serve the API.

One Pi process (or guest) per session. After 15 minutes idle
(`APIPI_IDLE_TTL`), kill the process. The session row stays. Resume
from the event log.

Cross-tenant IDs return `404`, not `403`.

## Tools

Function tools, MCP, and skills. See [tools](tools.md).
