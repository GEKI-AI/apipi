#!/bin/sh
set -eu
if [ -f /tmp/apipi-pins.env ]; then
  # shellcheck disable=SC1091
  . /tmp/apipi-pins.env
fi
: "${PINNED_AGENT_BROWSER:?}"
: "${PINNED_AGENT_BROWSER_SHA256:?}"
: "${PINNED_CHROME:?}"
: "${PINNED_CHROME_SHA256:?}"
cd /tmp
curl -fsSL -o agent-browser \
  "https://github.com/vercel-labs/agent-browser/releases/download/v${PINNED_AGENT_BROWSER}/agent-browser-linux-x64"
echo "${PINNED_AGENT_BROWSER_SHA256}  agent-browser" | sha256sum -c -
install -m 755 agent-browser /usr/local/bin/agent-browser
rm -f agent-browser
agent-browser --version | grep -q "${PINNED_AGENT_BROWSER}"
curl -fsSL -o chrome.zip \
  "https://storage.googleapis.com/chrome-for-testing-public/${PINNED_CHROME}/linux64/chrome-headless-shell-linux64.zip"
echo "${PINNED_CHROME_SHA256}  chrome.zip" | sha256sum -c -
rm -rf /opt/chrome-headless-shell /tmp/chrome-unpack
mkdir -p /opt/chrome-headless-shell /tmp/chrome-unpack
unzip -q chrome.zip -d /tmp/chrome-unpack
cp -a /tmp/chrome-unpack/chrome-headless-shell-linux64/. /opt/chrome-headless-shell/
chmod 755 /opt/chrome-headless-shell/chrome-headless-shell
rm -rf chrome.zip /tmp/chrome-unpack
if ldd /opt/chrome-headless-shell/chrome-headless-shell | grep -q 'not found'; then
  ldd /opt/chrome-headless-shell/chrome-headless-shell >&2
  echo "chrome-headless-shell is missing libraries" >&2
  exit 1
fi
fc-cache -f
mkdir -p /etc/apipi
cat > /etc/apipi/browser.env <<'EOF'
AGENT_BROWSER_EXECUTABLE_PATH=/opt/chrome-headless-shell/chrome-headless-shell
AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage
AGENT_BROWSER_IDLE_TIMEOUT_MS=0
AGENT_BROWSER_NO_WEBMCP=1
AGENT_BROWSER_SOCKET_DIR=/tmp/agent-browser
AGENT_BROWSER_SCREENSHOT_DIR=/workspace/.browser/screenshots
AGENT_BROWSER_DOWNLOAD_PATH=/workspace/.browser/downloads
EOF
rm -f /tmp/apipi-pins.env
