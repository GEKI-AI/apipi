# 0007. Run modes

Where Pi and stdio MCP run.

| Mode | Isolation |
| --- | --- |
| `host` | None. Child of the gateway. Not for production. |
| `jail` | Linux namespaces (bubblewrap + cgroup + pasta). Shared kernel. |
| `microvm` | KVM guest (Firecracker + jailer). Own kernel. |

Default: `jail`. Set `APIPI_RUN_MODE`. If the mode cannot start, the
process exits. No fallback.

`host`, `jail`, and `microvm` are implemented. `jail` still exits if
`bwrap`, `pasta`, or cgroup v2 cannot start. `microvm` still exits if
`/dev/kvm`, `firecracker`, `jailer`, the kernel and rootfs images,
`ip`, or `iptables` cannot start. Operators without jail tools must
set `APIPI_RUN_MODE=host`.

`jail` uses pasta so Pi can reach the model URL and HTTP MCP with no
host loopback to Postgres. Other session directories under
`APIPI_SESSIONS_DIR` are a tmpfs; only the current session directory
is bind-mounted. `microvm` attaches a TAP device for egress. Neither
mode falls back to the other.

`host` logs a warning: not suited for production.

`jail` is for self-host and internal multi-tenant use. Tenants must
not write the host, hit Postgres on loopback, or read each other's
session directories. The jail still shares the host kernel and the
gateway UID, so it does not protect the host from a hostile user.
`microvm` does (hardware virt). Still not a full QEMU PC.

Production is systemd on the host. Docker Compose starts Postgres
only. Nested jail or microvm inside a container is not the production
path.

The `openai_hosted` workspace is packed into a microvm guest at boot.
Before the guest exits, the gateway pulls the workspace back to the
host folder so the next pack still has those files. Files under
`artifacts/` and `outputs/` are also published to the host artifact
store when a turn completes.

## Same server (default)

The intended same-server default is Pi in `jail`, files in a session
directory next to Pi. That is `environment.openai_hosted` — a local
folder, not OpenAI's cloud.

`none` turns file tools off. `self_hosted` puts the computer on a
runner you attach. Remote works with all three run modes.

Operator install, systemd, and storage are in [run modes](../../docs/run-modes.md).
