# 0007. Run modes

Where Pi and stdio MCP run.

| Mode | Isolation |
| --- | --- |
| `host` | None. Child of the gateway. Not for production. |
| `jail` | Linux namespaces (bubblewrap + cgroup + pasta). Shared kernel. Fallback when microvm cannot run. |
| `microvm` | KVM guest (Firecracker + jailer). Own kernel. Production when a computer is in use. |

Process default: `jail`. Production SaaS and enterprise set
`APIPI_RUN_MODE=microvm`. If the mode cannot start, the process exits
before it binds HTTP. No fallback.

`host`, `jail`, and `microvm` are implemented. `jail` still exits if
`bwrap`, `pasta`, or cgroup v2 cannot start, or if a throwaway jail
cannot launch. `microvm` still exits if `/dev/kvm`, `firecracker`,
`jailer`, the kernel and rootfs images, `ip`, or `iptables` cannot
start, or if a throwaway guest cannot boot. Operators without jail
tools must set `APIPI_RUN_MODE=host`.

`jail` uses pasta so Pi can reach the model URL and HTTP MCP with no
host loopback to Postgres. Other session directories under
`APIPI_SESSIONS_DIR` are a tmpfs; only the current session directory
is bind-mounted. `microvm` attaches a TAP device for egress. Neither
mode falls back to the other.

`host` logs a warning: not suited for production.

`jail` is for lab, CI, and hosts that cannot run Firecracker (no KVM,
nested Docker, an uncontrolled VM). Shared kernel is not enough for
multi-tenant SaaS or untrusted enterprise workloads. `microvm` is the
production isolation when a computer is in use. It protects the host
from a hostile session (hardware virt). Still not a full QEMU PC.

When the computer is local (`openai_hosted` or the `hosted` alias), Pi
and the session files share one jail or guest. Do not split them. The
only supported split is `self_hosted`: Pi stays in the run mode, and
the runner is elsewhere. The customer must sandbox the runner.

Production is systemd on the host. Docker Compose starts Postgres
only. Nested jail or microvm inside a container is not the production
path.

The `openai_hosted` workspace is packed into a microvm guest at boot.
Before the guest exits, the gateway pulls the workspace back to the
host folder so the next pack still has those files. Files under
`artifacts/` and `outputs/` are also published to the host artifact
store when a turn completes.

## Same server (default)

The intended same-server production path is Pi in `microvm`, files in
a session directory next to Pi. That is `environment.openai_hosted` —
a local folder, not OpenAI's cloud. `hosted` is an alias for the same
folder.

`none` turns file tools off. `self_hosted` puts the computer on a
runner you attach. Remote works with all three run modes.

Operator install, systemd, and storage are in [run modes](../../docs/run-modes.md).
