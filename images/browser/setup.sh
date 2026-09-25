#!/bin/sh
set -eu
PIN=0.0.82
PREFIX=/opt/apipi/playwright-mcp
mkdir -p "$PREFIX" /var/cache/npm
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_cache=/var/cache/npm
export npm_config_update_notifier=false
npm install --prefix "$PREFIX" --ignore-scripts --omit=dev "@playwright/mcp@${PIN}"
test -f "$PREFIX/node_modules/@playwright/mcp/cli.js"
