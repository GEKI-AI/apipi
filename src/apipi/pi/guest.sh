#!/bin/sh
export PATH="/usr/local/bin:/usr/bin:/bin"
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t tmpfs tmpfs /tmp
if command -v ip >/dev/null 2>&1; then
  ip link set lo up 2>/dev/null || true
elif command -v ifconfig >/dev/null 2>&1; then
  ifconfig lo up 2>/dev/null || true
fi
mkdir -p /tmp/workspace
if [ -b /dev/vdb ]; then
  tar -xf /dev/vdb -C /tmp/workspace
fi
if [ -f /tmp/workspace/.apipi/net ]; then
  . /tmp/workspace/.apipi/net
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
cd /tmp/workspace
if [ -f /tmp/workspace/.apipi/env ]; then
  set -a
  . /tmp/workspace/.apipi/env
  set +a
fi
export HOME=/tmp/workspace
if [ -f /tmp/workspace/.apipi/setup.sh ] && [ ! -f /tmp/workspace/.apipi/setup.done ]; then
  if ! /bin/sh /tmp/workspace/.apipi/setup.sh > /tmp/workspace/.apipi/setup.log 2>&1; then
    echo "environment setup failed" >&2
    cat /tmp/workspace/.apipi/setup.log >&2
    exit 1
  fi
  echo ok > /tmp/workspace/.apipi/setup.done
fi
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
