# 0007. Run modes

Where Pi and stdio MCP run.

| Mode | Isolation |
| --- | --- |
| `host` | None. Child of the gateway. Not for production. |
| `jail` | Linux namespaces (bubblewrap + cgroup + pasta). Shared kernel. |
| `microvm` | KVM guest (Firecracker + jailer). Own kernel. |

Default: `jail`. Set `APIPI_RUN_MODE`. If the mode cannot start, the
process exits. No fallback.

Only `host` is implemented today. `jail` and `microvm` exit at
startup. Serve with `APIPI_RUN_MODE=host` until those modes exist.

`host` logs a warning: not suited for production.

`jail` does not protect the host from a hostile user. `microvm` does
(hardware virt). Still not a full QEMU PC.

## Same server (default)

The intended same-server default is Pi in `jail`, files in a session
directory next to Pi. That is `environment.openai_hosted` — a local
folder, not OpenAI's cloud.

`none` turns file tools off. `self_hosted` puts the computer on a
runner you attach. Remote works with all three run modes.
