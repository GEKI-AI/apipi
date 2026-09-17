#!/usr/bin/env bash
# Run every session example against a gateway that is already up.
# Client OPENAI_BASE_URL is the ApiPi gateway. APIPI_MODEL must exist
# on the model host the gateway process uses.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Set OPENAI_API_KEY to a bearer the gateway will accept." >&2
  exit 1
fi
if [ -z "${OPENAI_BASE_URL:-}" ]; then
  echo "Set OPENAI_BASE_URL to the ApiPi gateway, for example http://127.0.0.1:8000/v1" >&2
  exit 1
fi
if [ -z "${APIPI_MODEL:-}" ]; then
  echo "Set APIPI_MODEL to a model id from GET /v1/models on that gateway." >&2
  exit 1
fi

OUT="${APIPI_EXAMPLES_OUT:-/tmp/opencode/apipi-examples}"
mkdir -p "$OUT"
fail=0

run() {
  local name="$1"
  shift
  echo "=== $name ==="
  if "$@"; then
    echo "=== $name ok ==="
  else
    echo "=== $name failed ===" >&2
    fail=1
  fi
}

run transform_file \
  env APIPI_OUTPUT="$OUT/fruits-sorted.txt" \
  uv run python examples/sessions/transform_file.py

run openai_sdk \
  uv run --with openai python examples/sessions/openai_sdk.py

run browser_screenshot \
  uv run --with openai python examples/sessions/browser_screenshot.py

if [ "$fail" -ne 0 ]; then
  echo "One or more session examples failed." >&2
  exit 1
fi
echo "All session examples completed."
