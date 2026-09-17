#!/usr/bin/env bash
# Manual split microvm run: one API, one worker, then every session example.
# Not pytest. Not GitHub CI. See docs/tests.md#manual-microvm-examples.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [ -z "${APIPI_MODEL:-}" ]; then
  echo "Set APIPI_MODEL to a model id the model host lists." >&2
  exit 1
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ ! -f "$ROOT/.env" ]; then
  echo "Set OPENAI_BASE_URL on this process (model host) or put it in .env." >&2
  echo "That value is the model host Pi calls, not http://127.0.0.1:8000/v1." >&2
  exit 1
fi

TOKEN="${APIPI_EXAMPLE_TOKEN:-dev-token}"
WORKER_TOKEN="${APIPI_WORKER_TOKEN:-local-worker}"
DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://apipi:apipi@127.0.0.1:5432/apipi}"
GATEWAY="http://127.0.0.1:8000"
LOG_DIR="${APIPI_EXAMPLES_OUT:-/tmp/opencode/apipi-examples}"
mkdir -p "$LOG_DIR"
API_LOG="$LOG_DIR/api.log"
WORKER_LOG="$LOG_DIR/worker.log"
API_PID=""
WORKER_PID=""

if curl -sf -m 1 http://127.0.0.1:8000/health >/dev/null; then
  echo "An API is already on :8000. Stop it, or run examples/sessions/run.sh against it." >&2
  exit 1
fi

if [ ! -e /dev/kvm ]; then
  echo "microvm needs /dev/kvm." >&2
  exit 1
fi
if ! command -v firecracker >/dev/null || ! command -v jailer >/dev/null; then
  echo "microvm needs firecracker and jailer on PATH. Run: uv run apipi install --microvm" >&2
  exit 1
fi
BROWSER_ROOTFS="${APIPI_MICROVM_ROOTFS_BROWSER:-$HOME/.cache/apipi/microvm/rootfs-browser.ext4}"
if [ ! -f "$BROWSER_ROOTFS" ]; then
  echo "browser_screenshot.py needs the browser rootfs at $BROWSER_ROOTFS" >&2
  echo "Run: uv run apipi install --microvm --image browser" >&2
  exit 1
fi
if ! sudo -n true 2>/dev/null; then
  echo "The worker needs passwordless sudo for TAP and jailer." >&2
  exit 1
fi

cleanup() {
  if [ -n "$WORKER_PID" ]; then
    sudo kill "$WORKER_PID" 2>/dev/null || true
  fi
  if [ -n "$API_PID" ]; then
    kill "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if command -v docker >/dev/null && [ -f "$ROOT/compose.yaml" ]; then
  echo "Starting Compose postgres."
  docker compose -f "$ROOT/compose.yaml" up -d postgres
fi

export DATABASE_URL
export APIPI_WORKER_TOKEN="$WORKER_TOKEN"
export PYTHONUNBUFFERED=1
uv run apipi migrate
uv run apipi check --role api

nohup uv run apipi serve --api-only --host 0.0.0.0 --port 8000 >>"$API_LOG" 2>&1 &
API_PID=$!
echo "API pid $API_PID log $API_LOG"

for _ in $(seq 1 40); do
  if curl -sf -m 1 "$GATEWAY/health" >/dev/null; then
    break
  fi
  sleep 0.25
done
if ! curl -sf -m 1 "$GATEWAY/health" >/dev/null; then
  echo "API did not become healthy. See $API_LOG" >&2
  exit 1
fi

touch "$WORKER_LOG"
chmod a+rw "$WORKER_LOG" 2>/dev/null || true
sudo -E env \
  DATABASE_URL="$DATABASE_URL" \
  APIPI_WORKER_TOKEN="$WORKER_TOKEN" \
  APIPI_API_URL="$GATEWAY" \
  APIPI_RUN_MODE=microvm \
  APIPI_SANDBOX_DEFAULT_SIZE=S \
  PYTHONUNBUFFERED=1 \
  PATH="$PATH" \
  HOME="$HOME" \
  nohup uv run apipi worker >>"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
echo "worker pid $WORKER_PID log $WORKER_LOG"

for _ in $(seq 1 90); do
  if grep -q 'worker hello' "$WORKER_LOG" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if ! grep -q 'worker hello' "$WORKER_LOG" 2>/dev/null; then
  echo "Worker did not register. See $WORKER_LOG" >&2
  exit 1
fi

echo "Gateway $GATEWAY (also http://192.168.0.49:8000 if you are on this host)."
export OPENAI_API_KEY="$TOKEN"
export OPENAI_BASE_URL="$GATEWAY/v1"
export APIPI_MODEL
"$ROOT/examples/sessions/run.sh"
