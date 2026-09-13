# 0007. Run modes

Where Pi and stdio MCP run.

| Mode | Isolation |
| --- | --- |
| `none` | None. Child of the gateway. Not for production. |
| `microvm` | KVM guest (Firecracker + jailer). Own kernel. Production when a computer is in use. |
| `package.mod:Class` | Operator-provided backend behind the same isolation interface. |

Process default: `none`. Production SaaS and enterprise set
`APIPI_RUN_MODE=microvm`. If the mode cannot start, the process exits
before it binds HTTP. No fallback.

`none` and `microvm` are the built-in backends. `microvm` still exits
if `/dev/kvm`, `firecracker`, `jailer`, the kernel and rootfs images,
`ip`, `iptables`, or `tc` cannot start, or if a throwaway guest cannot
boot. Operators without KVM must set `APIPI_RUN_MODE=none`. `host` and
`jail` are not valid and fail at startup.

`microvm` attaches a TAP device for egress. That TAP is allowlisted
(model host, session HTTP MCP hosts, extra operator hosts, and DNS)
and rate-limited with `tc`. Unlisted destinations are dropped.

`none` logs a warning: not suited for production.

`microvm` is the production isolation when a computer is in use. It
protects the host from a hostile session (hardware virt). Still not a
full QEMU PC.

When the computer is local (`openai_hosted` or the `hosted` alias), Pi
and the session files share one guest (or the host process in `none`).
Do not split them. The only supported split is `self_hosted`: Pi stays
in the run mode, and the runner is elsewhere. The customer must
sandbox the runner.

Production is systemd on the host. Docker Compose starts Postgres
only. Nested microvm inside a container is not the production path.

The `openai_hosted` workspace is packed into a microvm guest at boot.
Before the guest exits, the gateway pulls the workspace back to the
host folder so the next pack still has those files. Files under
`artifacts/` and `outputs/` are also published to the host artifact
store when a turn completes.

A custom backend implements the isolation interface (`require`,
`probe`, `spawn`, plus flags for probe, stdio placement, and the
production warning) and is selected with `APIPI_RUN_MODE`.

## Same server (default)

The intended same-server production path is Pi in `microvm`, files in
a session directory next to Pi. That is `environment.openai_hosted` —
a local folder, not OpenAI's cloud. `hosted` is an alias for the same
folder.

`environment.type=none` turns file tools off. Isolation `none` is the
un-sandboxed Pi process. `self_hosted` puts the computer on a runner
you attach. Remote works with every run mode.

Operator install, systemd, and storage are in [run modes](../../docs/run-modes.md).
