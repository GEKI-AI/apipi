#!/bin/sh
export PATH="/usr/local/bin:/usr/bin:/bin"
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t tmpfs tmpfs /tmp
mkdir -p /tmp/workspace
if [ -b /dev/vdb ]; then
  tar -xf /dev/vdb -C /tmp/workspace
fi
cd /tmp/workspace
if [ -f /tmp/workspace/.apipi/env ]; then
  set -a
  . /tmp/workspace/.apipi/env
  set +a
fi
export HOME=/tmp/workspace
if [ -f /tmp/workspace/.apipi/guest.py ] && command -v python3 >/dev/null 2>&1; then
  exec python3 /tmp/workspace/.apipi/guest.py
fi
if command -v socat >/dev/null 2>&1; then
  cmd="pi --mode rpc --no-session"
  if [ -f /tmp/workspace/.apipi/pi-cmd ]; then
    cmd=$(cat /tmp/workspace/.apipi/pi-cmd)
  fi
  exec socat VSOCK-LISTEN:52,reuseaddr EXEC:"$cmd",stderr
fi
echo "APIPI_RUN_MODE=microvm guest needs python3 or socat" >&2
exit 1
