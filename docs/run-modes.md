# Run modes

Run mode is where Pi and stdio MCP run. It is server config
(`APIPI_RUN_MODE`), not an OpenAI field. Environment is a separate
choice: where file and shell tools run. See
[environments](environments.md). A remote runner does not replace Pi
isolation. The gateway always stays on the host.

If the selected mode cannot start, `apipi serve` exits before it binds
HTTP. There is no silent fallback. For `jail` and `microvm` the
process also launches a throwaway sandbox and tears it down. That
probe must succeed before the API listens.

When the computer is local (`openai_hosted` or the `hosted` alias), Pi
and the session files share that jail or guest. The only supported
split is `self_hosted`: Pi stays in the run mode, and the runner is
elsewhere. The customer must sandbox the runner. Tests that do not
need a computer can use `environment.type=none`.

| Mode | When to use | Isolation |
| --- | --- | --- |
| `host` | Local tests and laptops without sandbox tools | None. Pi is a child of the gateway. Not for production. |
| `jail` | Fallback when microvm cannot run: no KVM, nested Docker, lab, CI | Linux namespaces. Shared kernel. Tenants cannot write the host, reach Postgres on loopback, or read other session directories. |
| `microvm` | SaaS and enterprise production when a computer is in use | KVM guest with its own kernel. Protects the host from a hostile session. |

The process default is `jail` so `apipi serve` can start on a machine
without KVM. That is a fallback, not the production posture.
Production operators set `APIPI_RUN_MODE=microvm`. If microvm cannot
launch, the process exits; it does not fall back to `jail` or `host`.
Operators without jail tools must set `APIPI_RUN_MODE=host`. `host`
logs a warning.

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

Do not vendor a distro in git. Build a rootfs on the operator machine:

```
./scripts/microvm-rootfs
```

That writes `rootfs.ext4` and, when the download works, a Firecracker
`vmlinux` under `$XDG_CACHE_HOME/apipi/microvm` (or `~/.cache/apipi/microvm`).
Pass a directory argument to choose another location. The script needs
`curl`, `tar`, `mkfs.ext4`, `mount`, and root (or `sudo`) for the
loop mount and chroot. It installs Alpine, Node, the pinned Pi CLI,
Python 3, `ip`, `socat`, and copies `src/apipi/pi/guest.sh` to
`/sbin/apipi-guest`.

```
export APIPI_MICROVM_ROOTFS="$HOME/.cache/apipi/microvm/rootfs.ext4"
export APIPI_MICROVM_KERNEL="$HOME/.cache/apipi/microvm/vmlinux"
```

If the kernel download fails, get a Firecracker-compatible `vmlinux`
from the [Firecracker getting started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
guide and point `APIPI_MICROVM_KERNEL` at it. Missing `/dev/kvm`,
binaries, images, `ip`, or `iptables` exits the process. How to run
the live microvm tests is in [tests](tests.md).

## Storage

Three stores. Do not mix them up.

| Store | What | Where it lives | Lifetime |
| --- | --- | --- | --- |
| **Session** | Transcript: events, turns, items, artifact metadata | Postgres | Until the session is deleted. A session [export](api.md#export) is the thread. |
| **Environment files** | The computer. File and shell tools. | `openai_hosted`: `{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}` next to Pi. `self_hosted`: the runner. `none`: no files. | `openai_hosted` lasts across Pi stop until `APIPI_WORKSPACE_TTL` (default 1 hour) with no session activity, or until the session is deleted. Runner files stay on the runner. |
| **Artifacts** | Named outputs the API can fetch | Metadata in Postgres. Bytes on the gateway host under `{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{session_id}/{id}`. | Until the artifact or session is deleted. `GET` content reads this store in every run mode. `410` if nothing was published. |

`APIPI_MAX_WORKSPACE_BYTES` (default 1GiB) caps one `openai_hosted`
directory. An oversized microvm pull is not unpacked onto the host.
`APIPI_MAX_ARTIFACT_BYTES` (default 512MiB) caps the published host
store for one session. Over those caps, harvest emits
`agent.session.error` with `workspace_too_large` or
`artifact_too_large`. See [config](config.md).

When a turn completes, the gateway copies files under `artifacts/` and
`outputs/` on that computer into the host store. Copies are immutable.
`GET` content works before Pi stops. Harvest on Pi stop is a safety
net for files written after the last completed turn. Killing idle Pi
does not delete the `openai_hosted` directory. After workspace TTL
with no activity, that directory is deleted and a later spawn recopies
skills into a fresh workspace. `host` and `jail` read the session
directory. `microvm` pulls a workspace tar over vsock before the guest
exits so the next pack is not empty. `self_hosted` reads `artifacts/`
and `outputs/` from the runner if it is connected. A crash before
publish can lose unpublished files.

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
unpacked onto a guest tmpfs, and is the guest cwd. Before the guest
exits, the gateway pulls the workspace back to the host folder so the
next pack still has those files. Skill paths from that workspace are
rewritten to `/tmp/workspace`.

RPC is JSON lines over vsock. Egress uses a TAP device and NAT. There
is no host loopback to Postgres.

This is the mode that protects the host from a hostile user. Guest RAM
is the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Chromium can
use its own sandbox inside the guest. Do not put Chromium in the
gateway.

## Production

Run `apipi serve` under systemd on the host with
`APIPI_RUN_MODE=microvm`. Keep secrets in an environment file that the
unit loads. One process: do not add uvicorn workers.

A typical microvm unit:

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
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

Many operators run that unit as root so jailer can chroot Firecracker
and the process can create TAP devices. Set `APIPI_MICROVM_KERNEL` and
`APIPI_MICROVM_ROOTFS` in the environment file.

A typical jail unit, only when microvm cannot run on that host:

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

## Docker

The Compose file at the repo root starts Postgres and publishes it on
the host. It does not start the gateway.

Running the gateway inside Docker is not the production path. Nested
user namespaces, cgroup delegation, TAP devices, and `/dev/kvm` each
need extra capabilities. A privileged container can be used in a lab.
It is not equivalent to systemd on the host.

Settings for run mode are in [configuration](config.md).
