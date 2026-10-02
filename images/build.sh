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
The guest is Debian trixie slim. There is no Alpine path.
Prefer `apipi images build <id>` which packages the output into a
versioned build directory for `apipi images push`.
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
OUT=${1:-"${XDG_CACHE_HOME:-$HOME/.cache}/apipi/image-build"}
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
OV_PIN=${PINNED_PI-}
# shellcheck disable=SC1091
source "$RECIPE/image.env"
if [[ -n "$OV_SIZE" ]]; then
  SIZE_MIB=$OV_SIZE
fi
PIN=${OV_PIN-}
PACKAGES=${PACKAGES-}
ARCHS=${ARCHS:-"x86_64 aarch64"}
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

pin_string() {
  sed -n "/^$1 = /,/^[^ ]/p" "$VERSION_PY" | sed -n 's/.*"\([^"]*\)".*/\1/p' | head -1
}

if [[ -z "$PIN" && -f "$VERSION_PY" ]]; then
  PIN=$(pin_string PINNED_PI)
fi
if [[ -z "$PIN" ]]; then
  echo "could not read PINNED_PI" >&2
  exit 1
fi
UV_VER=$(pin_string PINNED_UV)
if [[ -z "$UV_VER" ]]; then
  echo "could not read PINNED_UV" >&2
  exit 1
fi
DEBIAN_DIGEST=$(pin_string PINNED_DEBIAN_DIGEST)
if [[ -z "$DEBIAN_DIGEST" ]]; then
  echo "could not read PINNED_DEBIAN_DIGEST" >&2
  exit 1
fi
NODE_VER=$(pin_string PINNED_NODE)
if [[ -z "$NODE_VER" ]]; then
  echo "could not read PINNED_NODE" >&2
  exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)
    DEB_ARCH=amd64
    PLATFORM=linux/amd64
    NODE_ARCH=x64
    NODE_SHA=$(pin_string PINNED_NODE_SHA256_X86_64)
    UV_SHA=$(pin_string PINNED_UV_SHA256_X86_64)
    ;;
  aarch64)
    DEB_ARCH=arm64
    PLATFORM=linux/arm64
    NODE_ARCH=arm64
    NODE_SHA=$(pin_string PINNED_NODE_SHA256_AARCH64)
    UV_SHA=$(pin_string PINNED_UV_SHA256_AARCH64)
    ;;
  *)
    echo "unsupported arch: $ARCH" >&2
    exit 1
    ;;
esac

case " ${ARCHS} " in
  *" ${ARCH} "*) ;;
  *)
    echo "image ${IMAGE_ID} is not built for ${ARCH}" >&2
    exit 1
    ;;
esac

if [[ -z "$NODE_SHA" ]]; then
  echo "could not read node sha256 for $ARCH" >&2
  exit 1
fi
if [[ -z "$UV_SHA" ]]; then
  echo "could not read uv sha256 for $ARCH" >&2
  exit 1
fi

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing $1" >&2
    exit 1
  }
}

need curl
need tar
need sha256sum
need mkfs.ext4
need mount
need umount
need xz

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
  if [[ -n "${DOCKER_CID:-}" ]]; then
    docker rm "$DOCKER_CID" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

MNT="${WORKDIR}/mnt"
IMG="${WORKDIR}/rootfs.ext4"
# shellcheck disable=SC1091
source "$(dirname "$0")/kernel.env"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${KERNEL_BUILD}/${ARCH}/vmlinux-${KERNEL_VERSION}"

truncate -s "${SIZE_MIB}M" "$IMG"
mkfs.ext4 -F -q "$IMG"
mkdir -p "$MNT"
as_root mount -o loop "$IMG" "$MNT"

bootstrap_debian() {
  local image="debian:trixie-slim@${DEBIAN_DIGEST}"
  if command -v docker >/dev/null 2>&1; then
    DOCKER_CID=$(docker create --platform "$PLATFORM" "$image")
    docker export "$DOCKER_CID" | as_root tar -xf - -C "$MNT"
    docker rm "$DOCKER_CID" >/dev/null
    DOCKER_CID=""
    return 0
  fi
  echo "docker is unavailable; falling back to debootstrap without the digest pin" >&2
  need debootstrap
  as_root debootstrap --variant=minbase --arch="$DEB_ARCH" trixie "$MNT" \
    http://deb.debian.org/debian
}

bootstrap_debian
as_root mkdir -p "$MNT/proc" "$MNT/sys" "$MNT/dev" "$MNT/sbin" "$MNT/workspace" "$MNT/tmp" "$MNT/usr/local"
as_root mount -t proc proc "$MNT/proc"
as_root mount -t sysfs sysfs "$MNT/sys"
as_root mount --bind /dev "$MNT/dev"
if [[ -f /etc/resolv.conf ]]; then
  as_root cp /etc/resolv.conf "$MNT/etc/resolv.conf"
fi
as_root cp "$GUEST_SH" "$MNT/sbin/apipi-guest"
as_root chmod 755 "$MNT/sbin/apipi-guest"

NODE_NAME="node-${NODE_VER}-linux-${NODE_ARCH}"
NODE_URL="https://nodejs.org/dist/${NODE_VER}/${NODE_NAME}.tar.xz"
curl -fsSL "$NODE_URL" -o "${WORKDIR}/node.tar.xz"
echo "${NODE_SHA}  ${WORKDIR}/node.tar.xz" | sha256sum -c -
mkdir -p "${WORKDIR}/node"
tar -xJf "${WORKDIR}/node.tar.xz" -C "${WORKDIR}/node" --strip-components=1
rm -rf "${WORKDIR}/node/include" "${WORKDIR}/node/share/doc" "${WORKDIR}/node/share/man"
as_root cp -a "${WORKDIR}/node/." "$MNT/usr/local/"

SETUP=""
if [[ -f "$RECIPE/setup.sh" ]]; then
  as_root cp "$RECIPE/setup.sh" "$MNT/tmp/apipi-image-setup.sh"
  as_root chmod 755 "$MNT/tmp/apipi-image-setup.sh"
  SETUP="/tmp/apipi-image-setup.sh"
fi
if [[ "$IMAGE_ID" == browser ]]; then
  as_root tee "$MNT/tmp/apipi-pins.env" >/dev/null <<EOF
PINNED_AGENT_BROWSER=$(pin_string PINNED_AGENT_BROWSER)
PINNED_AGENT_BROWSER_SHA256=$(pin_string PINNED_AGENT_BROWSER_SHA256_X86_64)
PINNED_CHROME=$(pin_string PINNED_CHROME_HEADLESS_SHELL)
PINNED_CHROME_SHA256=$(pin_string PINNED_CHROME_HEADLESS_SHELL_SHA256_X86_64)
EOF
fi

as_root chroot "$MNT" /bin/sh -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git iproute2 socat ca-certificates tar ripgrep ${PACKAGES}
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  npm install -g --ignore-scripts @earendil-works/pi-coding-agent@${PIN}
  if [ -n '${SETUP}' ]; then
    /bin/sh '${SETUP}'
    rm -f '${SETUP}'
  fi
"

UV_NAME="uv-${ARCH}-unknown-linux-musl"
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VER}/${UV_NAME}.tar.gz"
curl -fsSL "$UV_URL" -o "${WORKDIR}/uv.tar.gz"
echo "${UV_SHA}  ${WORKDIR}/uv.tar.gz" | sha256sum -c -
tar -xzf "${WORKDIR}/uv.tar.gz" -C "$WORKDIR"
as_root mkdir -p "$MNT/usr/local/bin"
as_root cp "$WORKDIR/$UV_NAME/uv" "$WORKDIR/$UV_NAME/uvx" "$MNT/usr/local/bin/"
as_root chmod 755 "$MNT/usr/local/bin/uv" "$MNT/usr/local/bin/uvx"
as_root tee "$MNT/etc/pip.conf" >/dev/null <<'EOF'
[global]
user = true
break-system-packages = true
EOF
as_root chroot "$MNT" /bin/sh -c '
  set -e
  node --version
  python3 --version
  pip --version
  uv --version
  uvx --version
  rg --version
  git --version
  curl --version
  socat -V >/dev/null
  ip -V
  HOME=/tmp/pip-user pip install --user six
  HOME=/tmp/pip-user python3 -c "import six"
  rm -rf /tmp/pip-user
'
as_root umount "$MNT/dev"
as_root umount "$MNT/sys"
as_root umount "$MNT/proc"
as_root umount "$MNT"
unset MNT
cp "$IMG" "$ROOTFS"

if curl -fsSL "$KERNEL_URL" -o "${KERNEL}.part"; then
  mv "${KERNEL}.part" "$KERNEL"
  printf '%s\n' "$KERNEL_VERSION" > "${KERNEL}.version"
else
  rm -f "${KERNEL}.part"
  echo "kernel download failed; set APIPI_MICROVM_KERNEL to a vmlinux file" >&2
fi

echo "rootfs: $ROOTFS"
if [[ -f "$KERNEL" ]]; then
  echo "kernel: $KERNEL"
fi
if [[ "$IMAGE_ID" == browser || "$IMAGE_ID" == default ]]; then
  echo "use \`apipi images build\` to package this output, then \`apipi images push --store-version <v>\` and \`apipi images pull $IMAGE_ID\`"
  echo "APIPI_MICROVM_KERNEL and APIPI_MICROVM_ROOTFS are dev-only overrides; leave them unset to use the image store."
else
  echo "use \`apipi images build\` or \`apipi images pull\`, then set sandbox_image=$IMAGE_ID"
  echo "do not export APIPI_MICROVM_ROOTFS to this file; that replaces the default image"
fi
