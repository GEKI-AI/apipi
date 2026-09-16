# Run modes

Run mode is where Pi and stdio MCP run (`APIPI_RUN_MODE`). Environment
is a separate choice: where file and shell tools run. A remote runner
leaves Pi isolation in place. The gateway always stays on the host.

Production isolation is a Firecracker microVM: each session gets its
own kernel so a hostile tenant cannot share the host kernel with the
gateway or with other sessions. That is stronger than a
shared-kernel container. Pi and the local computer share that guest.
The HTTP API never runs inside it.

If the selected mode cannot start, `apipi serve` exits before it binds
HTTP. The process never switches to another mode on its own. For
`microvm` and for a custom backend that sets `needs_probe`, the process
also launches a throwaway sandbox and tears it down. That probe must
succeed before the API listens. `apipi serve --api-only` skips that
probe so a rootless API host does not need `/dev/kvm`. Sandbox guests
then belong on `apipi worker`.

When the computer is local (`openai_hosted` or the `hosted` alias), Pi
and the session files share that isolation boundary. The only supported
split is `self_hosted`: Pi stays in the run mode, and the runner is
elsewhere. The customer must sandbox the runner. Tests that do not
need a computer can use `environment.type=none`. That environment value
means “no files.” Isolation `none` means “no sandbox for Pi.” They are
not the same setting.

| Mode | When to use | Isolation |
| --- | --- | --- |
| `none` | Local tests and laptops without a sandbox | Pi is a child of the gateway. Use `microvm` in production. |
| `microvm` | SaaS and enterprise production when a computer is in use | KVM guest with its own kernel. Protects the host from a hostile session. |
| `package.mod:Class` | An operator-provided backend | Whatever that class implements. Missing import fails at startup. |

The process default is `none` so `apipi serve` can start without KVM.
Production operators set `APIPI_RUN_MODE=microvm`. If the microVM cannot
launch, the process exits. `none` logs a warning. Valid built-in names
are `none` and `microvm`.

Run production under systemd on the host, next to Pi. Docker Compose
in this repo starts Postgres only. Nested microVM inside a container
is a lab setup. Production isolation is systemd on the host with
`APIPI_RUN_MODE=microvm`.

## What to install

Every mode needs Python 3.13, [uv](https://docs.astral.sh/uv/),
the store, the Pi CLI (`pi --mode rpc`) on `PATH` at version 0.85.1, and
`OPENAI_BASE_URL`. `apipi serve` exits if those are missing. See
[install](install.md). The extra OS packages differ by mode.

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
| Guest rootfs | `APIPI_MICROVM_ROOTFS` (ext4) for `APIPI_MICROVM_IMAGE=default`. Include Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest` from `src/apipi/pi/guest.sh`. Optional `APIPI_MICROVM_ROOTFS_BROWSER` when `image` is `browser`. |
| TAP / NAT | Permission to create a TAP device, set `ip_forward`, and add iptables rules. Root or `CAP_NET_ADMIN` is the usual setup. |

`apipi install --microvm` downloads Firecracker and jailer and builds
the guest image. Build a rootfs on the operator machine yourself if
you want another output directory. Two flavors:

| Flavor | Command | Output |
| --- | --- | --- |
| `default` | `./scripts/microvm-rootfs` | `rootfs.ext4` |
| `browser` | `./scripts/microvm-rootfs --flavor browser` | `rootfs-browser.ext4` |

Both write a Firecracker `vmlinux` (when the download works) under
`$XDG_CACHE_HOME/apipi/microvm` (or `~/.cache/apipi/microvm`). Pass a
directory argument to choose another location. The files do not
overwrite each other. The script needs `curl`, `tar`, `mkfs.ext4`,
`mount`, and root (or `sudo`) for the loop mount and chroot.

`default` installs Alpine, Node, the pinned Pi CLI, Python 3, `ip`,
`socat`, and copies `src/apipi/pi/guest.sh` to `/sbin/apipi-guest`.
`browser` is that image plus Alpine Chromium and font/NSS packages so
stdio MCP such as Playwright can drive a **system** browser
(`/usr/bin/chromium-browser`). Playwright's own glibc browser builds
do not run on this musl guest. The image is 4 GiB unless you set
`SIZE_MIB`. Raise `APIPI_MICROVM_MEM_MIB` to 1024–2048 for browser
guests. See [production sizing](production.md#sizing).

```
apipi install --microvm
apipi install --microvm --image browser
```

```
./scripts/microvm-rootfs
./scripts/microvm-rootfs --flavor browser
```

When `APIPI_MICROVM_KERNEL` and `APIPI_MICROVM_ROOTFS` (or the browser
rootfs) are unset, the process uses those cache files if they exist.
Env, `.env`, and `[sandbox].kernel` / `rootfs` still override. The
install command also prints `export` lines. Production should set
explicit paths.

`APIPI_MICROVM_IMAGE` (`default` or `browser`) selects which rootfs
the process boots. The choice is process-wide, not per session.
Missing path for the selected image exits at startup. Session
`packages` and `setup_commands` still run on whichever image you
booted; flavors are the heavy, stable base.

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
| **Session** | Transcript: events, turns, items, artifact metadata | SQLite for one process; Postgres when the store is shared | Until the session is deleted. A session [export](api.md#export) is the thread. |
| **Environment files** | The computer. File and shell tools. | `openai_hosted`: `{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}` next to Pi. Guest cwd is `/workspace`. `self_hosted`: the runner. `none`: no files. | `openai_hosted` is ephemeral: sandbox TTL (default 1 hour) stops Pi and deletes scratch files, or the session is deleted. Runner files stay on the runner. |
| **Artifacts** | Named outputs the API can fetch | Metadata in the store. Bytes in `APIPI_ARTIFACT_STORE`: local files under `{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{key_id}/{session_id}/{id}`, or an S3-compatible bucket with the same key layout. | Until the artifact or session is deleted. `GET` content reads this store in every run mode. `410` if nothing was published. |

`APIPI_MAX_WORKSPACE_BYTES` (default 1GiB) caps one `openai_hosted`
directory. An oversized microvm pull is not unpacked onto the host.
`APIPI_MAX_ARTIFACT_BYTES` (default 512MiB) caps the published host
store for one session. Over those caps, harvest emits
`agent.session.error` with `workspace_too_large` or
`artifact_too_large`. See [config](config.md).

When a turn completes, the gateway copies files under `artifacts/` and
`outputs/` on that computer into the host store. Copies are immutable.
`GET` content works before Pi stops. Harvest on Pi stop is a safety
net for files written after the last completed turn. After
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with no activity,
Pi stops and the `openai_hosted` directory is deleted. A later turn
rehydrates skills, packages, and setup commands into a fresh
`/workspace`. Isolation `none` reads the session directory on the
host. `microvm` unpacks onto guest `/workspace`. `self_hosted` reads
`artifacts/` and `outputs/` from the runner if it is connected. A
crash before publish can lose unpublished files. The gateway cannot
delete files on a remote runner.

## `none`

Pi is a child of `apipi serve`. There is no namespace, cgroup, or
guest. Use this when a microvm cannot run. Use `microvm` in production.
The process logs a warning.

## `microvm`

[Firecracker](https://firecracker-microvm.github.io/) is a KVM
hypervisor built for short-lived microVMs. ApiPi uses it so a session
that can run shell and file tools cannot take the host: the guest has
its own kernel, the gateway never enters that guest, and Pi, stdio
MCP, and local file tools boot inside it.

The session directory is packed into a workspace drive at boot,
unpacked onto a guest tmpfs at `/workspace`, and is the guest cwd.
Scratch files do not survive sandbox stop. Skill paths from that
workspace are rewritten to `/workspace`.

The guest kernel needs entropy before Pi can open TLS to the model.
Firecracker attaches a virtio-rng device, and the workspace includes
host random that guest init credits into `/dev/urandom`. Without that,
Linux 4.14 `getrandom()` blocks and the turn stays in progress.

RPC is JSON lines over vsock. Egress uses a TAP device and NAT. There
is no host loopback to Postgres. By default that TAP is fail-closed:
the guest may reach the model host from `OPENAI_BASE_URL`, HTTP MCP
hosts for that session, extra hosts in `APIPI_MICROVM_EGRESS_HOSTS`,
package registries when `environment.packages` is set (PyPI, npm,
Alpine), and DNS (`1.1.1.1` and `8.8.8.8`). Other TCP is rejected. The gateway
connects HTTP MCP from the host first; Pi still dials the same URLs
from the guest, so those hosts must be allowed. Set
`APIPI_MICROVM_EGRESS_ALLOWLIST=off` only in a lab. Each TAP is also
rate-limited with `tc` (`APIPI_MICROVM_EGRESS_MBIT`, default 50). See
[config](config.md).

This is the mode that protects the host from a hostile session. Guest
RAM is the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Chromium
can use its own sandbox inside the guest.

## Debug the guest

`apipi microvm shell` boots the same Firecracker guest that agent
sessions use: same kernel, rootfs flavor, jailer, TAP, and egress
allowlist. It attaches your terminal to the serial console. It does
not bind HTTP and does not create a tenant session. Use it to inspect
the image, run `pi` on the CLI, and debug networking.

```
apipi install --microvm
apipi microvm shell
apipi microvm shell --config /etc/apipi.toml
apipi microvm shell --image browser --workspace /path/to/files
```

`--image` selects `default` or `browser` for this VM only. Unset, it
follows `APIPI_MICROVM_IMAGE`. `--workspace` packs a host directory
into guest `/workspace` the same way `openai_hosted` does. Unset, the
guest gets an empty scratch workspace.

Requirements match `apipi check` without `--fast`: KVM, Firecracker,
jailer, `ip`, `iptables`, `tc`, and the selected kernel and rootfs.
The command does not need `APIPI_RUN_MODE=microvm`. Unset image paths
use the cache files from `apipi install --microvm` when they exist.
If you are not root, the command re-runs itself with `sudo -E`, the
absolute interpreter, and `PATH` / `HOME` kept. It does not run
`sudo uv`. Creating a TAP device, NAT rules, and `ip_forward` needs
root or `CAP_NET_ADMIN`. Jailer needs root to chroot Firecracker.
Misconfiguration fails with a message that names the failed step
(missing binary, missing image, or missing rights) and does not hang.
The command needs a TTY.

The guest cwd is `/workspace`. Pi is on `PATH`. Type `exit` or press
Ctrl-C to stop the VM. TAP devices, jailer chroot, and temp dirs are
removed the same way a session kill does.

This is an operator and lab tool. The TAP egress allowlist still
applies. Leave the allowlist on unless this lab already turns it off.
Agent spawn is unchanged.

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

Production isolation is Firecracker on a **worker host**. The API
process should be `apipi serve --api-only` and does not need KVM.
Combined `apipi serve` (no `--api-only`) is the single-host embedded
worker: it still probes the run mode and can create TAP devices on
that box. Host sizing, overprovision, and drain are in
[production](production.md). Several API hosts need sticky routing
only while Pi is still in-process; workers remove that for live Pi.
See [multiple nodes](scale.md) and [sandbox workers](workers.md).

A typical worker unit (KVM and TAP stay here):

```
[Unit]
Description=ApiPi worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/apipi
EnvironmentFile=/etc/apipi.env
ExecStart=/opt/apipi/.venv/bin/apipi worker --config /etc/apipi.toml
Restart=on-failure
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

Many operators run that unit as root so jailer can chroot Firecracker
and the process can create TAP devices. Set `APIPI_MICROVM_KERNEL`,
`APIPI_MICROVM_ROOTFS`, `APIPI_WORKER_TOKEN`, and `APIPI_API_URL` in
the environment file. The API unit is `apipi serve --api-only` with
no DeviceAllow for KVM.

## Docker

The Compose file at the repo root starts Postgres and an API service
that runs `apipi serve --api-only` without privileged mode, `/dev/kvm`,
or TAP. That is the supported container path. Firecracker stays on a
host `apipi worker` unit (`deploy/systemd/apipi-worker.service`). Nested
microVM inside Docker is a lab setup only.

The API process is public HTTP. Workers connect outbound to
`/internal/worker`. Do not publish the worker. Tenant `self_hosted`
runners are a different socket and a different secret.

Sandbox backend, guest images, RAM, vCPUs, and TAP egress are in
[configuration](config.md#sandbox).
