# Run modes

Run mode is where Pi and stdio MCP run. It is server config
(`APIPI_RUN_MODE`), not an OpenAI field. Environment is a separate
choice: where file and shell tools run. See
[environments](environments.md). A remote runner does not replace Pi
isolation. The gateway always stays on the host.

If the selected mode cannot start, `apipi serve` exits. There is no
silent fallback.

| Mode | When to use | Isolation |
| --- | --- | --- |
| `host` | Local tests and laptops without jail tools | None. Pi is a child of the gateway. |
| `jail` | Default. Self-host and internal multi-tenant | Linux namespaces. Shared kernel. Tenants cannot write the host, reach Postgres on loopback, or read other session directories. |
| `microvm` | Public or otherwise hostile tenants | KVM guest with its own kernel. Protects the host from a hostile session. |

`jail` is the configured default. Operators without jail tools must
set `APIPI_RUN_MODE=host`. `host` logs a warning and is not suited for
production.

Production is systemd on the host next to Pi. Docker Compose in this
repo starts Postgres only. Nested jail or microvm inside a container
is a lab setup, not the production path.

## What to install

Every mode needs Python 3.13, [uv](https://docs.astral.sh/uv/),
Postgres, the Pi CLI (`pi --mode rpc`) on `PATH`, and a model URL.
See [install](install.md). The extra OS packages differ by mode.

### `host`

Nothing beyond the gateway requirements. This mode is for development
and CI that cannot start a jail.

### `jail`

Linux with cgroup v2 and unprivileged user namespaces. Install
bubblewrap and pasta:

```
# Debian / Ubuntu
sudo apt-get install -y bubblewrap passt

# Fedora
sudo dnf install -y bubblewrap passt
```

`pasta` is in the `passt` package. The service user must be able to
create a child cgroup and set `memory.max`. On systemd, set
`Delegate=yes` on the unit (see [Production](#production)). If
`bwrap`, `pasta`, or cgroup v2 cannot start, the process exits.

### `microvm`

Linux with `/dev/kvm`. Install Firecracker, jailer, and host net
tools, and point at operator-provided guest images:

| Need | Typical package or setting |
| --- | --- |
| Firecracker and jailer | Binaries from the [Firecracker release](https://github.com/firecracker-microvm/firecracker/releases) on `PATH` |
| `ip` | `iproute2` |
| `iptables` | `iptables` |
| Guest kernel | `APIPI_MICROVM_KERNEL` (a `vmlinux` file) |
| Guest rootfs | `APIPI_MICROVM_ROOTFS` (ext4). Include Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest` from `src/apipi/pi/guest.sh`. |
| TAP / NAT | Permission to create a TAP device, set `ip_forward`, and add iptables rules. Root or `CAP_NET_ADMIN` is the usual setup. |

Do not vendor a distro in git. Missing `/dev/kvm`, binaries, images,
`ip`, or `iptables` exits the process.

## Storage

Three stores. Do not mix them up.

| Store | What | Where it lives | Lifetime |
| --- | --- | --- | --- |
| **Session** | Transcript: events, turns, items, artifact metadata | Postgres | Until the session is deleted. A session [export](api.md#export) is the thread. |
| **Environment files** | The computer. File and shell tools. | `openai_hosted`: `{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}` next to Pi. `self_hosted`: the runner. `none`: no files. | Scratch for `openai_hosted`: gone when Pi stops and when the session is deleted. Runner files stay on the runner. |
| **Artifacts** | Named outputs the API can fetch | Metadata in Postgres. Bytes on the gateway host under `{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{session_id}/{id}`. | Until the artifact or session is deleted. `GET` content reads this store in every run mode. `410` if nothing was published. |

When Pi stops, the gateway copies files under `artifacts/` on that
computer into the host store, then deletes the `openai_hosted`
workspace. `host` and `jail` copy from the session directory.
`microvm` pulls a tar over vsock while the guest is still up.
`self_hosted` reads `artifacts/` from the runner if it is connected.
A crash before stop can lose unpublished files. A later spawn recopies
skills into a fresh workspace.

## `host`

Pi is a child of `apipi serve`. There is no namespace, cgroup, or
guest. Use this when jail tools are missing. Do not use it in
production.

## `jail`

bubblewrap, pasta, and cgroup v2. The gateway never enters the jail.
Pi, stdio MCP, and local file tools run inside.

The session directory is bind-mounted read-write and is Pi's cwd. The
rest of `APIPI_SESSIONS_DIR` is a tmpfs, so a jailed process cannot
read other tenants' session directories. The host filesystem is still
visible read-only (binaries, `/usr`). The jail runs as the gateway
UID. Do not leave tenant data or secrets in files outside
`APIPI_SESSIONS_DIR` if a session should not see them.

Pasta gives the jail a network namespace with no host loopback, so Pi
cannot reach Postgres on localhost. Egress for the model URL and HTTP
MCP goes through pasta. This is not a VM. A kernel exploit can still
reach the host.

Chromium inside the jail needs `--no-sandbox`. Memory is capped with
cgroup `memory.max` (`APIPI_JAIL_MEMORY`, default 512M).

## `microvm`

[Firecracker](https://firecracker-microvm.github.io/) and jailer. The
gateway never enters the guest. Pi, stdio MCP, and local file tools
boot in a KVM guest with its own kernel.

The session directory is packed into a workspace drive at boot,
unpacked onto a guest tmpfs, and is the guest cwd. Writes stay in the
guest. They are not copied back to the host folder. Skill paths from
that workspace are rewritten to `/tmp/workspace`.

RPC is JSON lines over vsock. Egress uses a TAP device and NAT. There
is no host loopback to Postgres.

This is the mode that protects the host from a hostile user. Guest RAM
is the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Chromium can
use its own sandbox inside the guest. Do not put Chromium in the
gateway.

## Production

Run `apipi serve` under systemd on the host. Keep secrets in an
environment file that the unit loads. One process: do not add uvicorn
workers.

A typical jail unit:

```
[Unit]
Description=ApiPi gateway
After=network.target postgresql.service

[Service]
Type=simple
WorkingDirectory=/opt/apipi
EnvironmentFile=/etc/apipi.env
ExecStart=/opt/apipi/.venv/bin/apipi serve --config /etc/apipi.toml
Restart=on-failure
Delegate=yes
DelegateControllers=memory pids

[Install]
WantedBy=multi-user.target
```

`Delegate=yes` lets the service create child cgroups for
`APIPI_JAIL_MEMORY`. Do not set `NoNewPrivileges=yes`; bubblewrap
needs user namespaces.

A typical microvm unit adds KVM and net:

```
[Service]
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
```

Many operators run the microvm unit as root so jailer can chroot
Firecracker and the process can create TAP devices. Set
`APIPI_MICROVM_KERNEL` and `APIPI_MICROVM_ROOTFS` in the environment
file.

## Docker

The Compose file at the repo root starts Postgres and publishes it on
the host. It does not start the gateway.

Running the gateway inside Docker is not the production path. Nested
user namespaces, cgroup delegation, TAP devices, and `/dev/kvm` each
need extra capabilities. A privileged container can be used in a lab.
It is not equivalent to systemd on the host.

## Tests

GitHub CI installs `bubblewrap` and `passt` and tries cgroup v2
delegation, then runs `pytest -m "not slow"`. That includes `host` e2e
and live jail tests when `bwrap`, `pasta`, and cgroup v2 can start.
If jail still cannot start, those tests skip. That skip is not a
fallback to `host`. Live microvm boots are local machines with KVM
only. Do not add Firecracker to GitHub.

Settings for run mode are in [configuration](config.md).
