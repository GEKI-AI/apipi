# Guest image recipes

Each subdirectory is one MicroVM guest image. `build.sh` is the only
build script. A recipe does not repeat the Debian, Node, or Pi install.
It only says what is different. Alpine is not supported.

## Files

| File | Required | What |
| --- | --- | --- |
| `image.env` | yes | Small shell file. `build.sh` sources it. |
| `setup.sh` | no | Extra steps inside the chroot, after `apt` and the Node tarball. `browser/setup.sh` installs pinned `agent-browser` and `chrome-headless-shell`. |

`image.env` fields:

| Field | What |
| --- | --- |
| `IMAGE_ID` | Same as the directory name. |
| `SIZE_MIB` | ext4 size in MiB. `SIZE_MIB` in the environment overrides this. |
| `PACKAGES` | Extra Debian packages, space-separated. The base set is always `python3`, `python3-pip`, `python3-venv`, `curl`, `git`, `iproute2`, `socat`, `ca-certificates`, `tar`, and `ripgrep`. Node comes from a pinned nodejs.org tarball, not the distro package. |
| `MIN_SIZE` | Smallest sandbox size this image is meant for (`S`, `M`, or `L`). |
| `MIN_VCPUS` | Optional vCPU floor. Spawn uses the larger of the size vCPU count and this value. `browser` sets `2`. |
| `ARCHS` | Optional space-separated arches. The default is `x86_64` and `aarch64`. `browser` is `x86_64` only. |
| `DESCRIPTION` | One line for operators. |

`PINNED_PI` is an environment override of the shared script, not a
recipe field. The Debian digest, Node pin, uv pin, agent-browser pin,
and Chrome pin are read from `src/apipi/worker/pi/version.py`. They
are not recipe fields.

Every image also installs a pinned static musl `uv` and `uvx` at
`/usr/local/bin`, and writes `/etc/pip.conf` with `user = true` and
`break-system-packages = true`. Guest init sets `HOME=/workspace` and
`UV_CACHE_DIR=/tmp/uv-cache`. `pip install <pkg>` then lands in
`/workspace/.local`, which Python imports without extra path setup.
That directory is on the workspace tmpfs, so it uses guest RAM.
`uv pip install --system` cannot write the read-only root. Use
`uv run --with <pkg> script.py` or
`uv venv --system-site-packages /tmp/venv` instead. The build checks
`pip --version`, `uv --version`, and a `pip install --user` into a
temporary home before it unmounts the image.

## Build

From a git checkout:

```
./images/build.sh default
./images/build.sh browser /tmp/apipi-images
```

`./scripts/microvm-rootfs` is a wrapper. `--flavor default` and
`--flavor browser` call the same script. Output names stay
`rootfs.ext4`, `rootfs-browser.ext4`, and `vmlinux` in
`$XDG_CACHE_HOME/apipi/microvm` (or `~/.cache/apipi/microvm`). Any other
id writes `rootfs-<id>.ext4`. The script does not print
`APIPI_MICROVM_ROOTFS` for that id. Copying that export would replace
the default image. Use `apipi images build` or `apipi images pull`,
then set `sandbox_image` to the id.

A local x86_64 `default` build used about 1.1 GiB of the 2048 MiB
filesystem. Node under `/usr/local` was about 636 MiB. A local
`browser` build used about 1.7 GiB of the 4096 MiB filesystem, so
both sizes still fit. `work` was not measured. The estimate is that
3072 MiB still fits. The GitHub Images workflow does not build `work`
or aarch64. Confirm `work` on a local build.

The script needs `curl`, `tar`, `xz`, `mkfs.ext4`, `mount`, and root
(or `sudo`) for the loop mount and chroot. It bootstraps
`debian:trixie-slim` at the pinned digest with `docker create` and
`docker export`. If Docker is missing, it falls back to `debootstrap`
and warns that the digest pin is not applied. `apipi install
--microvm` runs it for you.

`apipi images build <id>` writes a manifest and a zstd rootfs into the
build directory. `apipi images push` uploads only the newest build of
each id and arch to `APIPI_IMAGE_SOURCE`, or to `--to`. `apipi images
publish` is the same command. Workers then run `apipi images pull`.
`apipi images list --remote` shows whether the local copy matches the
store. See [install](../docs/install.md).

`work` is the business-document image. `MIN_SIZE` is `M` and
`SIZE_MIB` is `3072`. Debian packages cover the compiled libraries.
`setup.sh` pins `python-docx`, `python-pptx`, and `fpdf2` into the
system site at build time, then writes one xlsx, docx, pptx, and pdf
and runs `pdftotext`. The image does not include LibreOffice or
pandoc. Select it with `sandbox_image=work`.

## Add an image

1. Create `images/<id>/image.env` with `IMAGE_ID` equal to `<id>`.
2. Set `SIZE_MIB`, `PACKAGES`, `MIN_SIZE`, and `DESCRIPTION`.
3. Add `setup.sh` only if packages are not enough.
4. Build with `./images/build.sh <id>`.

Do not edit `build.sh` for one image. Shared install steps belong there.
Package lists belong in the recipe.

## Update policy

Pins live in `src/apipi/worker/pi/version.py`. Bump the Debian digest,
Node, agent-browser, and chrome-headless-shell together about once a
month. Bump Chrome promptly on a Chrome security release. Each bump
needs a new image build and an operator re-pull. Re-read the
agent-browser changelog, re-derive `skills/browser/SKILL.md` from
`skill-data/core` at the same tag, and run `apipi images check`.

## Disabled browser features

| Feature | What we do | Why |
| --- | --- | --- |
| Stream WebSocket | Leave it on. It binds `127.0.0.1`. | Upstream always starts it. There is no env switch at daemon start. |
| Dashboard | Do not set `AGENT_BROWSER_DASHBOARD`. | Already off. |
| WebMCP | `AGENT_BROWSER_NO_WEBMCP=1` | Experimental, and ApiPi does not expose it. |
| Cloud providers | Do not set `AGENT_BROWSER_PROVIDER`. | Already off. |
| Update check / telemetry | Nothing to disable. Do not run `upgrade`. | No automatic check was found. The binary is read-only. |
| `agent-browser install` | Not used. Chrome comes from `EXECUTABLE_PATH`. | That command fetches the latest Chrome. |

## Check an image

This is a manual developer check. It is not part of `./scripts/check`,
GitHub CI, or `images.yml`. Build the image first.

```
apipi images check default
apipi images check browser
apipi images check browser --boot
```

The first command loop-mounts the rootfs read-only and checks Node
(at least 22.19), `pi`, `python3`, `pip`, `uv`, `rg`, `git`, `curl`,
`socat`, and `ip`. The browser check also requires `agent-browser
--version` to match the pin, the chrome-headless-shell binary, and
`/etc/apipi/browser.env`. It mounts `/dev` before it creates `shm` and
`pts`, so those directories are not written into the read-only image.
It needs root or sudo for the mount.

`--boot` is browser only. It also needs KVM, Firecracker, and sudo for
the TAP device. It boots the browser image at size `L`, opens a local
page, checks `snapshot -i`, a screenshot under `/workspace/.browser`,
a PDF, and that listeners are loopback-only. It also checks that Pi
sees the `browser` skill and no `mcp_playwright_*` tools. It fails if
Pi logs `pi.extension_error`.
