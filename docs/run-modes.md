# Run modes

Run mode is where Pi and MCP run (`APIPI_RUN_MODE`). Environment
is a separate choice: where file and shell tools run. Pi and the computer always share one isolation boundary, and there is no split. The gateway always stays on the host.
Why the modes exist, and what a microVM contains, is in
[isolation](isolation.md).

Production isolation is a Firecracker microVM on a worker (or on
combined `apipi serve`). Each session gets its own kernel so a hostile
tenant cannot share the host kernel with the gateway or with other
sessions. Pi and the local computer share that guest. The HTTP API
never runs inside it.

If the selected mode cannot start, `apipi serve` exits before it binds
HTTP. The process never switches to another mode on its own. For
`microvm` and for a custom backend that sets `needs_probe`, the process
also launches a throwaway sandbox and tears it down. That probe must
succeed before the API listens. `apipi serve --api-only` skips that
probe so a rootless API host does not need `/dev/kvm`. Sandbox guests
then belong on `apipi worker`.

Pi and the computer always share one isolation boundary, and there is no split. Tests that do not
need a computer can use `environment.type=none`. That environment value
means “no files.” Isolation `none` means “no sandbox for Pi.” They are
not the same setting. On `apipi serve --api-only`, Agents sessions with
`environment.type=none` are placed on workers that advertised `chat`
unless you set `APIPI_ENV_NONE_PLACEMENT` to `microvm` or `reject`. See
[workers](workers.md).

| Mode | When to use | Isolation |
| --- | --- | --- |
| `none` | Local tests and laptops without a sandbox | Pi is a child of the gateway. Use `microvm` in production. Logs a production warning. |
| `chat` | Dedicated chat worker pools | Same light Pi-on-host backend as `none`, with process name `chat` for placement and metrics. No production warning. |
| `microvm` | SaaS and enterprise production when a computer is in use | KVM guest with its own kernel. Protects the host from a hostile session. |
| `package.mod:Class` | An operator-provided backend | Whatever that class implements. Missing import fails at startup. |

The process default is `none` so `apipi serve` can start without KVM.
Production operators set `APIPI_RUN_MODE=microvm` on computer workers
and `APIPI_RUN_MODE=chat` on chat workers. Fleet layout is in
[chat fleets](chat.md). If the
microVM cannot launch, that process exits. `none` logs a warning. Valid
built-in names are `none`, `chat`, and `microvm`.

Run production as `apipi serve --api-only` plus `apipi worker` on the
host. Docker Compose can run the API without privileged mode. Nested
microVM inside a container is a lab setup.

## What to install

Every mode needs Python 3.13, [uv](https://docs.astral.sh/uv/),
the store, the Pi CLI (`pi --mode rpc`) on `PATH` at version 0.99.1, and
`OPENAI_BASE_URL`. `apipi serve` exits if those are missing. See
[install](install.md). The extra OS packages differ by mode.

### `none`

Nothing beyond the gateway requirements. This mode is for development
and CI that cannot start a microvm.

### `chat`

Same install as `none`. Use this on dedicated chat workers so they
advertise `chat` for placement. Pi still runs as a child of the worker
process.

### `microvm`

Linux with `/dev/kvm`. Install Firecracker, jailer, and host net
tools, and point at operator-provided guest images:

| Need | Typical package or setting |
| --- | --- |
| Firecracker and jailer | Binaries from the [Firecracker release](https://github.com/firecracker-microvm/firecracker/releases) on `PATH` |
| `ip` and `tc` | `iproute2` |
| `iptables` | `iptables` |
| Guest kernel | From the image store (`apipi images pull`). `APIPI_MICROVM_KERNEL` is a dev-only override (a `vmlinux` file). |
| Guest rootfs | From the image store (`apipi images pull <id>`). `APIPI_MICROVM_ROOTFS` is a dev-only override (ext4). Include Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest` from `src/apipi/worker/pi/guest.sh`. |
| TAP / NAT | Permission to create a TAP device, set `ip_forward`, and add iptables rules. Root or `CAP_NET_ADMIN` is the usual setup. |

`apipi install --microvm` downloads Firecracker and jailer and pulls
a guest image when `APIPI_IMAGE_SOURCE` is set. The runtime uses the
image store only: `<id>/current` in the images dir. Missing files should be fixed with `apipi images pull`. Build a rootfs with `apipi images build` if
you want another image. Three flavors:

Recipes live in `images/<id>/`. `images/build.sh` is the build
script. `./scripts/microvm-rootfs` maps `--flavor` to that script so
older commands still work.

| Flavor | Command | Output |
| --- | --- | --- |
| `default` | `./images/build.sh default` | `rootfs.ext4` |
| `browser` | `./images/build.sh browser` | `rootfs-browser.ext4` |
| `work` | `./images/build.sh work` | `rootfs-work.ext4` |

Each build writes a Firecracker `vmlinux` (when the download works) into
the build output. The
kernel is Linux 6.1.186 from the Firecracker 1.17 CI set
(`images/kernel.env`). It is the newest 6.1 guest that release
validates, and it includes virtio-rng. Pass a directory argument to
`./images/build.sh` to choose another location, or use
`apipi images build <id> --out <dir>` for a versioned build directory.
script needs `curl`, `tar`, `mkfs.ext4`, `mount`, and root (or `sudo`)
for the loop mount and chroot.

`default` installs Debian trixie slim, a pinned Node tarball, the
pinned Pi CLI, Python 3.13, `pip`, a pinned `uv`, `ip`, `socat`,
`curl`, `git`, and `ripgrep`, and copies
`src/apipi/worker/pi/guest.sh` to `/sbin/apipi-guest`. The image sets
`pip` to install into the user site. With `HOME=/workspace`,
`pip install <pkg>` lands in `/workspace/.local` and is importable.
That uses guest RAM. `uv run --with <pkg> script.py` and
`uv venv --system-site-packages /tmp/venv` also work. `uv pip install
--system` does not, because the root filesystem is read-only.
`work` is that image plus libraries for Excel, Word, PowerPoint, PDF,
CSV, and charts. Compiled pieces come from Debian (`pandas`,
`matplotlib`, `pillow`, `lxml`, and others). `python-docx`,
`python-pptx`, and `fpdf2` are installed at build time so the pins
stay ahead of the distro packages. The image is 3 GiB. Use sandbox
size `M` or larger. It does not include LibreOffice or pandoc. Do not
point `APIPI_MICROVM_ROOTFS` at `rootfs-work.ext4`. That replaces the
default image. Use `sandbox_image=work` after `apipi images pull` or
`apipi install --microvm --image work`.
`browser` is that image plus agent-browser, a pinned
chrome-headless-shell, and Noto fonts (including CJK and emoji). It
is x86_64 only. The model uses bash and the built-in `browser` skill.
It does not install Playwright MCP. The image is 4 GiB unless you set
`SIZE_MIB`. Browser guests get at least 2 vCPUs, including size `M`.
Size `L` is 2 GiB of guest RAM. Rebuild after this change with
`apipi install --microvm --image browser`. See [install](install.md)
and [production sizing](production.md#sizing).

Guest init (`/sbin/apipi-guest`, from `guest.sh`) mounts `/proc`,
`/sys`, devtmpfs on `/dev`, tmpfs on `/dev/shm` (mode 1777), devpts
on `/dev/pts`, and tmpfs on `/tmp`. There is no fstab.

`apipi images check browser` is a manual check of a built browser
rootfs. It is not part of CI. `--boot` also boots the image and needs
KVM. How to run it is in `images/README.md` in the git checkout.

```
apipi install --microvm
apipi install --microvm --image browser
```

```
./images/build.sh default
./images/build.sh browser
./scripts/microvm-rootfs --flavor browser
```

Prebuilt images use the store format in
[ADR 0012](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0012-guest-image-store.md).
A version names the Pi pin, the Debian base digest, the Node pin,
`guest.sh`, and the recipe.
The sha256 names the bytes.

`APIPI_MICROVM_KERNEL` and `APIPI_MICROVM_ROOTFS` are dev-only overrides.
Leave them unset to use the image store.

Live session guests pick a rootfs from `sandbox_image`, not from size
alone. When the image is omitted, size `L` selects `browser` and other
sizes use the default image. Pull each image a worker
accepts with `apipi images pull <id>`. A missing image for the resolved id fails clearly; the
process does not fall back to another image. `apipi microvm shell --image`
selects the image for that VM, defaulting to `[sandbox].default_image`.
To make every session browser-class without callers setting an image,
set `[sandbox].default_size = "L"` (and size `worker_memory_mb` for ~2
GiB guests) or set `[sandbox].default_image = "browser"` with a size of
at least `M`. Image `browser` packs the built-in `browser` skill and
expects `agent-browser` in the guest. Session
`packages` and `setup_commands` still run on whichever image that
session booted.

If the kernel download fails, get a Firecracker 6.1 `vmlinux` and
point `APIPI_MICROVM_KERNEL` at it. The pin is `images/kernel.env`.
Missing `/dev/kvm`,
binaries, images, `ip`, `iptables`, or `tc` exits the process. How to run
the live microvm tests is in [tests](tests.md).

## Storage

Session state, environment files, artifacts, and the harness session
cache are different stores. Mixing them up leads to the wrong lifetime
and the wrong machine.

| Store | What | Where it lives | Lifetime |
| --- | --- | --- | --- |
| **Session** | Transcript: events, turns, items, artifact metadata | SQLite for one process; Postgres when the store is shared | Until the session is deleted. A session [export](api.md#export) is the thread. |
| **Harness session cache** | Pi's conversation file so a new process can continue the thread | Bytes in `APIPI_ARTIFACT_STORE` under the same session prefix as artifacts. The session row holds `pi_session_id`, size, and a full URI (`file://…` locally or `s3://bucket/key` on S3). Not listed on `GET …/artifacts`. | Until the session is deleted. Reloaded into a fresh `/workspace` on the next turn from that URI. |
| **Environment files** | The computer. File and shell tools. | `openai_hosted`: `{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}` next to Pi. Guest cwd is `/workspace`. `none`: no files. | `openai_hosted` is ephemeral: sandbox TTL (default 1 hour) stops Pi and deletes scratch files, or the session is deleted. |
| **Artifacts** | Named outputs the API can fetch | Metadata in the store. Bytes in `APIPI_ARTIFACT_STORE`: local files under `{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{key_id}/{session_id}/{id}`, or an S3-compatible bucket with the same key layout. Hosted file and skill bytes use the same backend under `files` and `skills` namespaces. | Until the artifact or session is deleted. `GET` content reads this store in every run mode. `410` if nothing was published. |

`APIPI_MAX_WORKSPACE_BYTES` (default 1GiB) caps one `openai_hosted`
directory. An oversized microvm pull is not unpacked onto the host.
`APIPI_MAX_ARTIFACT_BYTES` (default 512MiB) caps the published host
store for one session. Over those caps, harvest emits
`agent.session.error` with `workspace_too_large` or
`artifact_too_large`. See [config](config.md).

When a turn completes, the gateway copies files under `outputs/` on
that computer into the host store. Copies are immutable. `GET` content
works before Pi stops. Harvest on Pi stop is a safety net for files
written after the last completed turn. After
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with no activity,
Pi stops and the `openai_hosted` directory is deleted. A later turn
rehydrates skills, packages, and setup commands into a fresh
`/workspace`, and reloads the harness session cache so Pi keeps the
conversation. Published artifact bytes stay in the artifact store;
they are not copied back into `/workspace`. Isolation `none` reads the session directory on the
host. `microvm` unpacks onto guest `/workspace`. A crash before publish
can lose unpublished files. Existing stores may still list rows whose path starts
with `artifacts/`. Those remain readable. New publishes use
`outputs/`.

## `none`

Pi is a child of `apipi serve` or `apipi worker`. There is no namespace,
cgroup, or guest. Host Pi (`none` and `chat`) starts in its own process
group. Idle reap, session end, and process shutdown send SIGTERM then
SIGKILL to that group so MCP children started by Pi do not linger.
Gateway-owned MCP is a sibling of Pi and is stopped separately.
`APIPI_PI_MEM_MIB` is an optional soft ceiling for one host Pi (Node
heap plus RSS kill). It is not a cgroup. Use this when a microvm
cannot run. Use `microvm` in production. The process logs a warning.

## `microvm`

[Firecracker](https://firecracker-microvm.github.io/) is a KVM
hypervisor built for short-lived microVMs. ApiPi uses it so a session
that can run shell and file tools cannot take the host: the guest has
its own kernel, the gateway never enters that guest, and Pi and
local file tools boot inside it.

The session directory is packed into a workspace drive at boot,
unpacked onto a guest tmpfs at `/workspace`, and is the guest cwd.
Scratch files do not survive sandbox stop. Skill paths from that
workspace are rewritten to `/workspace`. The root filesystem is
attached read-only. `packages.python` and `packages.npm` install onto
that tmpfs, so they use guest RAM and count against the sandbox size.
`packages.system` cannot install there. Bake system packages into a
guest image. See [environments](environments.md).

The guest kernel needs entropy before Pi can open TLS to the model.
The pinned 6.1 kernel has virtio-rng (`CONFIG_HW_RANDOM_VIRTIO`), and
Firecracker attaches that device. Guest init still credits host random
into `/dev/urandom` so `getrandom()` does not block if the device is
late.

RPC is JSON lines over vsock. Egress uses a TAP device and NAT. Guest
localhost works. There is no host loopback to Postgres. By default the
guest may use the public internet. Private and special-use IPv4 ranges
are rejected. The TAP subnet stays open for the host broker, so the
model host is reached through that broker even when it is private.
Each TAP is rate-limited with
`tc` (`APIPI_MICROVM_EGRESS_MBIT`, default 50).

To lock destinations, set `APIPI_MICROVM_EGRESS_ALLOWLIST=on`. Then the
guest may reach only the model host, HTTP MCP hosts for that session,
extra hosts in `APIPI_MICROVM_EGRESS_HOSTS`, package registries when
`environment.packages` is set (PyPI, npm, Debian), and DNS (`1.1.1.1`
and `8.8.8.8`). Other TCP is rejected. See [config](config.md).

This is the mode that protects the host from a hostile session. Guest
RAM is the real cost (`APIPI_MICROVM_MEM_MIB`, default 512). Chromium
can use its own sandbox inside the guest.

## Debug the guest

`apipi microvm shell` boots the same Firecracker guest that agent
sessions use: same kernel, rootfs flavor, jailer, TAP, and egress
policy. It attaches your terminal to the serial console. It does
not bind HTTP and does not create a tenant session. Use it to inspect
the image, run `pi` on the CLI, and debug networking.

```
apipi install --microvm
apipi microvm shell
apipi microvm shell --config /etc/apipi.toml
apipi microvm shell --image browser --workspace /path/to/files
```

`--image` selects the image for this VM only. Unset, it
follows `[sandbox].default_image`. `--workspace` packs a host directory
into guest `/workspace` the same way `openai_hosted` does. Unset, the
guest gets an empty scratch workspace.

Requirements match `apipi check` without `--fast`: KVM, Firecracker,
jailer, `ip`, `iptables`, `tc`, and the selected image in the images dir
(`apipi images pull`). The command does not need `APIPI_RUN_MODE=microvm`.
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

This is an operator and lab tool. TAP egress matches agent sessions:
public internet, private IPv4 rejected, optional allowlist, same `tc`
rate. Agent
spawn is unchanged.

## Custom isolation

Operators and embedders can implement another isolation backend
without forking the gateway. The built-in names stay `none` and
`microvm`. A custom backend is selected with the same setting:

```
APIPI_RUN_MODE=package.mod:Class
```

The attribute must be a class, a zero-argument factory, or an instance.
It needs `name`, `needs_probe`,
`warn_not_production`, `require`, `probe`, and `spawn`. `spawn` starts
Pi and returns the RPC process. If `needs_probe` is true, startup
launches a throwaway sandbox before HTTP listen, the same way
`microvm` does. A missing module or attribute fails at startup with
`APIPI_RUN_MODE backend not found`. `host` and `jail` are not aliases
for a custom backend.

`name` is what usage events store as `run_mode`. Pick a short stable
string.

A small wrapper around `none` is `examples/isolation.py`. Put your
module on `PYTHONPATH`.

## Production

Production isolation is Firecracker on a **worker host**. The API
process should be `apipi serve --api-only` and does not need KVM.
Combined `apipi serve` (no `--api-only`) is the single-host embedded
worker: it still probes the run mode and can create TAP devices on
that box. Host sizing, overprovision, and drain are in
[production](production.md). Combined serve still needs sticky routing
when you run more than one process. API-only plus workers does not,
for live Pi. See [multiple nodes](scale.md) and
[workers](worker-concepts.md).

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
KillMode=control-group
TimeoutStopSec=15
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

`KillMode=control-group` is required so `systemctl stop` and
`Restart=on-failure` kill host Pi children, not only the worker PID.
`KillMode=process` leaks Pi after a crash. Chat workers use the same
unit and omit the KVM `DeviceAllow` lines. For a drain wait on stop,
install `deploy/systemd/apipi-worker-drain.conf` as
`TimeoutStopSec=16min` so SIGTERM can empty live Pi before SIGKILL.

Many operators run that unit as root so jailer can chroot Firecracker
and the process can create TAP devices. Set `APIPI_WORKER_TOKEN` and `APIPI_API_URL` in
the environment file. The API unit is `apipi serve --api-only` with
no DeviceAllow for KVM.

## Docker

The Compose file at the repo root starts Postgres and an API service
that runs `apipi serve --api-only` without privileged mode, `/dev/kvm`,
or TAP. That is the supported container path. Firecracker stays on a
host `apipi worker` unit (`deploy/systemd/apipi-worker.service`). Nested
microVM inside Docker is a lab setup only.

The API process is public HTTP. Workers connect outbound to
`/internal/worker`. Do not publish the worker.

Sandbox backend, guest images, RAM, vCPUs, and TAP egress are in
[configuration](config.md#sandbox).
