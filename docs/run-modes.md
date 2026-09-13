# Run modes

Run mode is where Pi and stdio MCP run (`APIPI_RUN_MODE`). Environment
is a separate choice: where file and shell tools run. A remote runner
does not replace Pi isolation. The gateway always stays on the host.

Production isolation is a Firecracker microVM: each session gets its
own kernel so a hostile tenant cannot share the host kernel with the
gateway or with other sessions. That is stronger than a
shared-kernel container. Pi and the local computer share that guest.
The HTTP API never runs inside it.

If the selected mode cannot start, `apipi serve` exits before it binds
HTTP. There is no silent fallback. For `microvm` and for a custom
backend that sets `needs_probe`, the process also launches a throwaway
sandbox and tears it down. That probe must succeed before the API
listens.

When the computer is local (`openai_hosted` or the `hosted` alias), Pi
and the session files share that isolation boundary. The only supported
split is `self_hosted`: Pi stays in the run mode, and the runner is
elsewhere. The customer must sandbox the runner. Tests that do not
need a computer can use `environment.type=none`. That environment value
means “no files.” Isolation `none` means “no sandbox for Pi.” They are
not the same setting.

| Mode | When to use | Isolation |
| --- | --- | --- |
| `none` | Local tests and laptops without a sandbox | None. Pi is a child of the gateway. Not for production. |
| `microvm` | SaaS and enterprise production when a computer is in use | KVM guest with its own kernel. Protects the host from a hostile session. |
| `package.mod:Class` | An operator-provided backend | Whatever that class implements. Missing import fails at startup. |

The process default is `none` so `apipi serve` can start without KVM.
Production operators set `APIPI_RUN_MODE=microvm`. If the microVM cannot
launch, the process exits rather than switching mode. `none` logs a
warning. `host` and `jail` are not valid run modes.

Run production under systemd on the host, next to Pi. Docker Compose
in this repo starts Postgres only. Nested microVM inside a container
is a lab setup, not the production path.

## What to install

Every mode needs Python 3.13, [uv](https://docs.astral.sh/uv/),
Postgres, the Pi CLI (`pi --mode rpc`) on `PATH`, and a model URL.
See [install](install.md). The extra OS packages differ by mode.

### `none`

Nothing beyond the gateway requirements. This mode is for development
and CI that cannot start a microvm.

### `microvm`

Linux with `/dev/kvm`. Install Firecracker, jailer, and host net
tools, and point at operator-provided guest images:

| Need | Typical package or setting |
| --- | --- |
| Firecracker and jailer | Binaries from the [Firecracker release](https://github.com/firecracker-microvm/firecracker/releases) on `PATH` |
| `ip` and `tc` | `iproute2` |
| `iptables` | `iptables` |
| Guest kernel | `APIPI_MICROVM_KERNEL` (a `vmlinux` file) |
| Guest rootfs | `APIPI_MICROVM_ROOTFS` (ext4). Include Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest` from `src/apipi/pi/guest.sh`. |
| TAP / NAT | Permission to create a TAP device, set `ip_forward`, and add iptables rules. Root or `CAP_NET_ADMIN` is the usual setup. |

Build a rootfs on the operator machine:

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
binaries, images, `ip`, `iptables`, or `tc` exits the process. How to run
the live microvm tests is in [tests](tests.md).

## Storage

Session state, environment files, and artifacts are three different
stores. Mixing them up leads to the wrong lifetime and the wrong
machine.

| Store | What | Where it lives | Lifetime |
| --- | --- | --- | --- |
| **Session** | Transcript: events, turns, items, artifact metadata | Postgres | Until the session is deleted. A session [export](api.md#export) is the thread. |
| **Environment files** | The computer. File and shell tools. | `openai_hosted`: `{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}` next to Pi. `self_hosted`: the runner. `none`: no files. | `openai_hosted` lasts across Pi stop until `APIPI_WORKSPACE_TTL` (default 1 hour) with no session activity, or until the session is deleted. Runner files stay on the runner. |
| **Artifacts** | Named outputs the API can fetch | Metadata in Postgres. Bytes in `APIPI_ARTIFACT_STORE`: local files under `{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{key_id}/{session_id}/{id}`, or an S3-compatible bucket with the same key layout. | Until the artifact or session is deleted. `GET` content reads this store in every run mode. `410` if nothing was published. |

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
skills into a fresh workspace. Isolation `none` reads the session
directory on the host. `microvm` pulls a workspace tar over vsock
before the guest exits so the next pack is not empty. `self_hosted`
reads `artifacts/` and `outputs/` from the runner if it is connected.
A crash before publish can lose unpublished files.

## `none`

Pi is a child of `apipi serve`. There is no namespace, cgroup, or
guest. Use this when a microvm cannot run. Do not use it in
production. The process logs a warning.

## `microvm`

[Firecracker](https://firecracker-microvm.github.io/) is a KVM
hypervisor built for short-lived microVMs. ApiPi uses it so a session
that can run shell and file tools cannot take the host: the guest has
its own kernel, the gateway never enters that guest, and Pi, stdio
MCP, and local file tools boot inside it.

The session directory is packed into a workspace drive at boot,
unpacked onto a guest tmpfs, and is the guest cwd. Before the guest
exits, the gateway pulls the workspace back to the host folder so the
next pack still has those files. Skill paths from that workspace are
rewritten to `/tmp/workspace`.

RPC is JSON lines over vsock. Egress uses a TAP device and NAT. There
is no host loopback to Postgres. By default that TAP is fail-closed:
the guest may reach the model host from `OPENAI_BASE_URL`, HTTP MCP
hosts for that session, extra hosts in `APIPI_MICROVM_EGRESS_HOSTS`,
and DNS (`1.1.1.1` and `8.8.8.8`). Other TCP is rejected. The gateway
connects HTTP MCP from the host first; Pi still dials the same URLs
from the guest, so those hosts must be allowed. Set
`APIPI_MICROVM_EGRESS_ALLOWLIST=off` only in a lab. Each TAP is also
rate-limited with `tc` (`APIPI_MICROVM_EGRESS_MBIT`, default 50). See
[config](config.md).

This is the mode that protects the host from a hostile session. Guest
RAM is the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Chromium
can use its own sandbox inside the guest.

## Custom isolation

Operators and embedders can implement another isolation backend
without forking the gateway. The built-in names stay `none` and
`microvm`. A custom backend is selected with the same setting:

```
APIPI_RUN_MODE=package.mod:Class
```

The attribute must be a class, a zero-argument factory, or an instance.
It needs `name`, `needs_probe`, `stdio_on_host`,
`warn_not_production`, `require`, `probe`, and `spawn`. `spawn` starts
Pi and returns the RPC process. If `needs_probe` is true, startup
launches a throwaway sandbox before HTTP listen, the same way
`microvm` does. A missing module or attribute fails at startup with
`APIPI_RUN_MODE backend not found`. `host` and `jail` are not aliases
for a custom backend.

`name` is what usage events store as `run_mode`. Pick a short stable
string. `stdio_on_host` controls whether stdio MCP is started next to
the gateway (`none` does this) or inside the sandbox with Pi
(`microvm` does this).

A small wrapper around `none` is `examples/isolation.py`. Put your
module on `PYTHONPATH`.

## Production

Run `apipi serve` under systemd on the host with
`APIPI_RUN_MODE=microvm`. Keep secrets in an environment file that the
unit loads. One process per host: do not add uvicorn workers. Host
sizing, overprovision, and drain are in [production](production.md).
Several hosts need sticky load balancing. See [multiple nodes](scale.md).

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

## Docker

The Compose file at the repo root starts Postgres and publishes it on
the host. It does not start the gateway.

Running the gateway inside Docker is not the production path. Nested
user namespaces, TAP devices, and `/dev/kvm` each need extra
capabilities. A privileged container can be used in a lab. It is not
equivalent to systemd on the host.

Sandbox backend, guest images, RAM, vCPUs, and TAP egress are in
[configuration](config.md#sandbox).
