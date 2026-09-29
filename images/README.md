# Guest image recipes

Each subdirectory is one MicroVM guest image. `build.sh` is the only
build script. A recipe does not repeat the Alpine, Node, or Pi install.
It only says what is different.

## Files

| File | Required | What |
| --- | --- | --- |
| `image.env` | yes | Small shell file. `build.sh` sources it. |
| `setup.sh` | no | Extra steps inside the chroot, after `apk` and `npm`. `browser/setup.sh` installs a pinned `@playwright/mcp` at `/opt/apipi/playwright-mcp`. |

`image.env` fields:

| Field | What |
| --- | --- |
| `IMAGE_ID` | Same as the directory name. |
| `SIZE_MIB` | ext4 size in MiB. `SIZE_MIB` in the environment overrides this. |
| `PACKAGES` | Extra Alpine packages, space-separated. The base set is always `nodejs`, `npm`, `python3`, `py3-pip`, `iproute2`, `socat`, `curl`, and `git`. |
| `MIN_SIZE` | Smallest sandbox size this image is meant for (`S`, `M`, or `L`). |
| `DESCRIPTION` | One line for operators. |

`ALPINE_VER` and `PINNED_PI` are environment overrides of the shared
script, not recipe fields. The default Alpine version is `3.21.3`. The
Pi pin is read from `src/apipi/worker/pi/version.py` when `PINNED_PI`
is unset. The uv pin and its sha256 values are read from that same
file. They are not recipe fields.

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

The script needs `curl`, `tar`, `mkfs.ext4`, `mount`, and root (or
`sudo`) for the loop mount and chroot. `apipi install --microvm` runs
it for you.

`apipi images build <id>` writes a manifest and a zstd rootfs into the
build directory. `apipi images push` uploads only the newest build of
each id and arch to `APIPI_IMAGE_SOURCE`, or to `--to`. `apipi images
publish` is the same command. Workers then run `apipi images pull`.
`apipi images list --remote` shows whether the local copy matches the
store. See [install](../docs/install.md).

`work` is the business-document image. `MIN_SIZE` is `M` and
`SIZE_MIB` is `3072`. Alpine packages cover the compiled libraries.
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

## Check the browser image

This is a manual developer check. It is not part of `./scripts/check`,
GitHub CI, or `images.yml`. Build the image first.

```
apipi images check browser
apipi images check browser --boot
```

The first command loop-mounts `rootfs-browser.ext4` (or `--rootfs`)
and runs `chromium-browser --headless --no-sandbox --dump-dom about:blank`
plus the vendored Playwright MCP `cli.js --help`. It needs root or
sudo for the mount.

`--boot` also needs KVM, Firecracker, and sudo for the TAP device. It
boots the browser image at size `L` with auto-inject, checks that
`mcp_playwright_*` tools registered, opens a `data:` URL, and writes
a screenshot under `outputs/`. It fails if Pi logs `pi.extension_error`.
