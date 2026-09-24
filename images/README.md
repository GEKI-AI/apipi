# Guest image recipes

Each subdirectory is one MicroVM guest image. `build.sh` is the only
build script. A recipe does not repeat the Alpine, Node, or Pi install.
It only says what is different.

## Files

| File | Required | What |
| --- | --- | --- |
| `image.env` | yes | Small shell file. `build.sh` sources it. |
| `setup.sh` | no | Extra steps inside the chroot, after `apk` and `npm`. |

`image.env` fields:

| Field | What |
| --- | --- |
| `IMAGE_ID` | Same as the directory name. |
| `SIZE_MIB` | ext4 size in MiB. `SIZE_MIB` in the environment overrides this. |
| `PACKAGES` | Extra Alpine packages, space-separated. The base set is always `nodejs`, `npm`, `python3`, `iproute2`, and `socat`. |
| `MIN_SIZE` | Smallest sandbox size this image is meant for (`S`, `M`, or `L`). |
| `DESCRIPTION` | One line for operators. |

`ALPINE_VER` and `PINNED_PI` are environment overrides of the shared
script, not recipe fields. The default Alpine version is `3.21.3`. The
Pi pin is read from `src/apipi/worker/pi/version.py` when `PINNED_PI`
is unset.

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
id writes `rootfs-<id>.ext4`.

The script needs `curl`, `tar`, `mkfs.ext4`, `mount`, and root (or
`sudo`) for the loop mount and chroot. `apipi install --microvm` runs
it for you.

## Add an image

1. Create `images/<id>/image.env` with `IMAGE_ID` equal to `<id>`.
2. Set `SIZE_MIB`, `PACKAGES`, `MIN_SIZE`, and `DESCRIPTION`.
3. Add `setup.sh` only if packages are not enough.
4. Build with `./images/build.sh <id>`.

Do not edit `build.sh` for one image. Shared install steps belong there.
Package lists belong in the recipe.
