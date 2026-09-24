#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)

if [[ -f "$HERE/../guest.sh" && -f "$HERE/../version.py" ]]; then
  GUEST_SH=$(cd "$HERE/.." && pwd)/guest.sh
  VERSION_PY=$(cd "$HERE/.." && pwd)/version.py
  IMAGES_DIR="$HERE"
elif [[ -f "$HERE/../src/apipi/worker/pi/guest.sh" ]]; then
  ROOT=$(cd "$HERE/.." && pwd)
  GUEST_SH="$ROOT/src/apipi/worker/pi/guest.sh"
  VERSION_PY="$ROOT/src/apipi/worker/pi/version.py"
  IMAGES_DIR="$HERE"
else
  echo "could not find guest.sh next to images/ or under src/apipi/worker/pi" >&2
  exit 1
fi

usage() {
  cat <<EOF
Usage: $0 <image-id|path-to-image-dir> [OUT_DIR]

Writes rootfs.ext4 for default, rootfs-browser.ext4 for browser, and
rootfs-<id>.ext4 for any other id. Also downloads vmlinux into OUT_DIR.
EOF
}

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  if [[ $# -lt 1 ]]; then
    exit 1
  fi
  exit 0
fi

TARGET=$1
shift
OUT=${1:-"${XDG_CACHE_HOME:-$HOME/.cache}/apipi/microvm"}
if [[ $# -gt 1 ]]; then
  echo "unexpected argument: $2" >&2
  usage >&2
  exit 1
fi

if [[ -d "$TARGET" && -f "$TARGET/image.env" ]]; then
  RECIPE=$(cd "$TARGET" && pwd)
elif [[ -d "$IMAGES_DIR/$TARGET" && -f "$IMAGES_DIR/$TARGET/image.env" ]]; then
  RECIPE="$IMAGES_DIR/$TARGET"
else
  echo "unknown image recipe: $TARGET" >&2
  exit 1
fi

OV_SIZE=${SIZE_MIB-}
OV_ALPINE=${ALPINE_VER-}
OV_PIN=${PINNED_PI-}
# shellcheck disable=SC1091
source "$RECIPE/image.env"
if [[ -n "$OV_SIZE" ]]; then
  SIZE_MIB=$OV_SIZE
fi
if [[ -n "$OV_ALPINE" ]]; then
  ALPINE_VER=$OV_ALPINE
fi
PIN=${OV_PIN-}
ALPINE_VER=${ALPINE_VER:-3.21.3}
PACKAGES=${PACKAGES-}
IMAGE_ID=${IMAGE_ID:-$(basename "$RECIPE")}
SIZE_MIB=${SIZE_MIB:-2048}

if [[ "$IMAGE_ID" != "$(basename "$RECIPE")" ]]; then
  echo "IMAGE_ID $IMAGE_ID does not match $(basename "$RECIPE")" >&2
  exit 1
fi

if [[ ! -f "$GUEST_SH" ]]; then
  echo "missing $GUEST_SH" >&2
  exit 1
fi

if [[ -z "$PIN" && -f "$VERSION_PY" ]]; then
  PIN=$(sed -n 's/^PINNED_PI = "\(.*\)"/\1/p' "$VERSION_PY")
fi
if [[ -z "$PIN" ]]; then
  echo "could not read PINNED_PI" >&2
  exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
  x86_64) ALPINE_ARCH=x86_64 ;;
  aarch64) ALPINE_ARCH=aarch64 ;;
  *)
    echo "unsupported arch: $ARCH" >&2
    exit 1
    ;;
esac

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing $1" >&2
    exit 1
  }
}

need curl
need tar
need mkfs.ext4
need mount
need umount

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

mkdir -p "$OUT"
case "$IMAGE_ID" in
  default) ROOTFS="${OUT}/rootfs.ext4" ;;
  browser) ROOTFS="${OUT}/rootfs-browser.ext4" ;;
  *) ROOTFS="${OUT}/rootfs-${IMAGE_ID}.ext4" ;;
esac
KERNEL="${OUT}/vmlinux"

WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/apipi-rootfs.XXXXXX")
cleanup() {
  if [[ -n "${MNT:-}" && -d "$MNT" ]]; then
    as_root umount "$MNT/proc" 2>/dev/null || true
    as_root umount "$MNT/sys" 2>/dev/null || true
    as_root umount "$MNT/dev" 2>/dev/null || true
    as_root umount "$MNT" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

ALPINE_TAR="${WORKDIR}/alpine.tar.gz"
MNT="${WORKDIR}/mnt"
IMG="${WORKDIR}/rootfs.ext4"
ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER%.*}/releases/${ALPINE_ARCH}/alpine-minirootfs-${ALPINE_VER}-${ALPINE_ARCH}.tar.gz"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/${ARCH}/kernels/vmlinux.bin"

curl -fsSL "$ALPINE_URL" -o "$ALPINE_TAR"
truncate -s "${SIZE_MIB}M" "$IMG"
mkfs.ext4 -F -q "$IMG"
mkdir -p "$MNT"
as_root mount -o loop "$IMG" "$MNT"
as_root tar -xzf "$ALPINE_TAR" -C "$MNT"
as_root mkdir -p "$MNT/proc" "$MNT/sys" "$MNT/dev" "$MNT/sbin" "$MNT/workspace" "$MNT/tmp"
as_root mount -t proc proc "$MNT/proc"
as_root mount -t sysfs sysfs "$MNT/sys"
as_root mount --bind /dev "$MNT/dev"
if [[ -f /etc/resolv.conf ]]; then
  as_root cp /etc/resolv.conf "$MNT/etc/resolv.conf"
fi
as_root cp "$GUEST_SH" "$MNT/sbin/apipi-guest"
as_root chmod 755 "$MNT/sbin/apipi-guest"
SETUP=""
if [[ -f "$RECIPE/setup.sh" ]]; then
  as_root cp "$RECIPE/setup.sh" "$MNT/tmp/apipi-image-setup.sh"
  as_root chmod 755 "$MNT/tmp/apipi-image-setup.sh"
  SETUP="/tmp/apipi-image-setup.sh"
fi
as_root chroot "$MNT" /bin/sh -c "
  set -e
  echo https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER%.*}/community >> /etc/apk/repositories
  apk add --no-cache nodejs npm python3 iproute2 socat ${PACKAGES}
  npm install -g --ignore-scripts @earendil-works/pi-coding-agent@${PIN}
  if [ -n '${SETUP}' ]; then
    /bin/sh '${SETUP}'
    rm -f '${SETUP}'
  fi
"
as_root umount "$MNT/dev"
as_root umount "$MNT/sys"
as_root umount "$MNT/proc"
as_root umount "$MNT"
unset MNT
cp "$IMG" "$ROOTFS"

if curl -fsSL "$KERNEL_URL" -o "${KERNEL}.part"; then
  mv "${KERNEL}.part" "$KERNEL"
else
  rm -f "${KERNEL}.part"
  echo "kernel download failed; set APIPI_MICROVM_KERNEL to a vmlinux file" >&2
fi

echo "rootfs: $ROOTFS"
if [[ -f "$KERNEL" ]]; then
  echo "kernel: $KERNEL"
fi
if [[ "$IMAGE_ID" == browser ]]; then
  echo "export APIPI_MICROVM_ROOTFS_BROWSER=$ROOTFS"
  echo "export APIPI_MICROVM_IMAGE=browser"
elif [[ "$IMAGE_ID" == default ]]; then
  echo "export APIPI_MICROVM_ROOTFS=$ROOTFS"
else
  echo "export APIPI_MICROVM_ROOTFS=$ROOTFS"
fi
if [[ -f "$KERNEL" ]]; then
  echo "export APIPI_MICROVM_KERNEL=$KERNEL"
fi
