#!/bin/sh
export PATH="/usr/local/bin:/usr/bin:/bin"
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/shm /dev/pts
SHM_SIZE=64m
if [ -f /etc/apipi/browser.env ]; then
  SHM_SIZE=512m
fi
mount -t tmpfs -o mode=1777,nosuid,nodev,size="$SHM_SIZE" tmpfs /dev/shm
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /tmp
WS=/workspace
if [ -d "$WS" ]; then
  mount -t tmpfs tmpfs "$WS"
else
  WS=/tmp/workspace
  mkdir -p "$WS"
fi
if command -v ip >/dev/null 2>&1; then
  ip link set lo up 2>/dev/null || true
elif command -v ifconfig >/dev/null 2>&1; then
  ifconfig lo up 2>/dev/null || true
fi
mkdir -p "$WS" "$WS/inputs"
if [ -b /dev/vdb ]; then
  tar -xf /dev/vdb -C "$WS"
fi
if [ -f "$WS/.apipi/net" ]; then
  . "$WS/.apipi/net"
  if command -v ip >/dev/null 2>&1; then
    ip link set eth0 up 2>/dev/null || true
    ip addr add "${GUEST_IP}/${GUEST_PREFIX}" dev eth0 2>/dev/null || true
    ip route add default via "${GUEST_GW}" 2>/dev/null || true
  elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig eth0 "${GUEST_IP}" netmask "${GUEST_MASK}" up 2>/dev/null || true
    route add default gw "${GUEST_GW}" 2>/dev/null || true
  fi
  printf "nameserver %s\nnameserver %s\n" "${GUEST_DNS:-1.1.1.1}" "${GUEST_DNS2:-8.8.8.8}" > /tmp/resolv.conf
  cp /tmp/resolv.conf /etc/resolv.conf 2>/dev/null || mount --bind /tmp/resolv.conf /etc/resolv.conf 2>/dev/null || true
fi
if [ -f /etc/apipi/browser.env ]; then
  set -a
  . /etc/apipi/browser.env
  set +a
  mkdir -p /tmp/agent-browser "$WS/.browser/screenshots" "$WS/.browser/downloads"
fi
cd "$WS"
if [ -f "$WS/.apipi/browser-check.sh" ]; then
  mkdir -p "$WS/outputs"
  /bin/sh "$WS/.apipi/browser-check.sh" || true
fi
if [ -f "$WS/.apipi/env" ]; then
  set -a
  . "$WS/.apipi/env"
  set +a
fi
export HOME="$WS"
mkdir -p /tmp/npm-cache
if [ -d /var/cache/npm ]; then
  cp -a /var/cache/npm/. /tmp/npm-cache/ 2>/dev/null || true
fi
export NPM_CONFIG_CACHE=/tmp/npm-cache
export npm_config_cache=/tmp/npm-cache
mkdir -p /tmp/uv-cache
export UV_CACHE_DIR=/tmp/uv-cache
if [ -f "$WS/.apipi/setup.sh" ] && [ ! -f "$WS/.apipi/setup.done" ]; then
  if ! /bin/sh "$WS/.apipi/setup.sh" > "$WS/.apipi/setup.log" 2>&1; then
    echo "environment setup failed" >&2
    cat "$WS/.apipi/setup.log" >&2
    exit 1
  fi
  echo ok > "$WS/.apipi/setup.done"
fi
if [ -d "$WS/.venv/bin" ]; then
  PATH="$WS/.venv/bin:$PATH"
fi
if [ -d "$WS/.npm/bin" ]; then
  PATH="$WS/.npm/bin:$PATH"
fi
export PATH
if [ -f "$WS/.apipi/guest.py" ] && command -v python3 >/dev/null 2>&1; then
  exec python3 "$WS/.apipi/guest.py"
fi
if command -v socat >/dev/null 2>&1; then
  cmd="pi --mode rpc --no-session"
  if [ -f "$WS/.apipi/pi-cmd" ]; then
    cmd=$(cat "$WS/.apipi/pi-cmd")
  fi
  exec socat VSOCK-LISTEN:52,reuseaddr EXEC:"$cmd",stderr
fi
echo "APIPI_RUN_MODE=microvm guest needs python3 or socat" >&2
exit 1
