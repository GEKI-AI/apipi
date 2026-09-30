import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from apipi.config import ConfigError, Settings
from apipi.worker.pi.proc import extension_error_text
from apipi.worker.pi.version import PINNED_AGENT_BROWSER

CHECK_SUDO_MARK = "APIPI_IMAGE_CHECK_SUDO"
CHECK_SUDO_NOTICE = "Need root for TAP, NAT, and jailer. Re-running under sudo."
BOOT_TIMEOUT_SEC = 180

_BASE_SCRIPT = r"""
set -eu
rootfs="$1"
mnt=$(mktemp -d)
cleanup() {
  umount "$mnt/dev/shm" 2>/dev/null || true
  umount "$mnt/dev/pts" 2>/dev/null || true
  umount "$mnt/dev" 2>/dev/null || true
  umount "$mnt/proc" 2>/dev/null || true
  umount "$mnt/tmp" 2>/dev/null || true
  umount "$mnt" 2>/dev/null || true
  rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
mount -o loop,ro "$rootfs" "$mnt"
mount -t proc proc "$mnt/proc"
mount -t tmpfs tmpfs "$mnt/tmp"
if ! mount -t devtmpfs devtmpfs "$mnt/dev"; then
  mount --bind /dev "$mnt/dev"
fi
mkdir -p "$mnt/dev/shm" "$mnt/dev/pts"
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs "$mnt/dev/shm"
mount -t devpts devpts "$mnt/dev/pts"
chroot "$mnt" /bin/sh -c '
  set -eu
  node --version
  pi --version
  python3 --version
  pip --version
  uv --version
  rg --version
  git --version
  curl --version
  socat -V >/dev/null
  ip -V
  node -p process.versions.node | awk -F. '{exit !($1>22 || ($1==22 && $2>=19))}'
'
echo "guest rootfs check ok"
"""

_BROWSER_SCRIPT = r"""
set -eu
rootfs="$1"
expect_browser="$2"
mnt=$(mktemp -d)
cleanup() {
  umount "$mnt/dev/shm" 2>/dev/null || true
  umount "$mnt/dev/pts" 2>/dev/null || true
  umount "$mnt/dev" 2>/dev/null || true
  umount "$mnt/proc" 2>/dev/null || true
  umount "$mnt/tmp" 2>/dev/null || true
  umount "$mnt" 2>/dev/null || true
  rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
mount -o loop,ro "$rootfs" "$mnt"
mount -t proc proc "$mnt/proc"
mount -t tmpfs tmpfs "$mnt/tmp"
if ! mount -t devtmpfs devtmpfs "$mnt/dev"; then
  mount --bind /dev "$mnt/dev"
fi
mkdir -p "$mnt/dev/shm" "$mnt/dev/pts"
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs "$mnt/dev/shm"
mount -t devpts devpts "$mnt/dev/pts"
chroot "$mnt" /bin/sh -c "
  set -eu
  test ! -e /opt/apipi/playwright-mcp
  test -x /opt/chrome-headless-shell/chrome-headless-shell
  test -f /etc/apipi/browser.env
  grep -q AGENT_BROWSER_NO_WEBMCP=1 /etc/apipi/browser.env
  ver=\$(agent-browser --version)
  echo \"\$ver\" | grep -q \"$expect_browser\"
  fc-list | grep -q .
"
echo "browser rootfs check ok"
"""

_BOOT_SCRIPT = r"""#!/bin/sh
set -eu
mkdir -p /workspace/outputs /workspace/.browser/screenshots /tmp/agent-browser
mkdir -p /tmp/apipi-page
printf '%s\n' '<!doctype html><title>apipi</title><button>Go</button>' \
  > /tmp/apipi-page/index.html
python3 - <<'PY'
import json
import socket
import subprocess
from pathlib import Path

report = {
    "version": "",
    "snapshot": False,
    "screenshot": "",
    "pdf": False,
    "loopback_only": False,
    "error": None,
}
try:
    version = subprocess.check_output(
        ["agent-browser", "--version"], text=True
    ).strip()
    report["version"] = version
    subprocess.check_call(
        ["agent-browser", "open", "file:///tmp/apipi-page/index.html"]
    )
    snap = subprocess.check_output(
        ["agent-browser", "snapshot", "-i"], text=True
    )
    report["snapshot"] = "button" in snap.lower() or "@e" in snap
    shot = Path("/workspace/.browser/screenshots/check.png")
    subprocess.check_call(["agent-browser", "screenshot", str(shot)])
    report["screenshot"] = str(shot) if shot.is_file() and shot.stat().st_size else ""
    pdf = Path("/workspace/.browser/check.pdf")
    subprocess.check_call(["agent-browser", "pdf", str(pdf)])
    report["pdf"] = pdf.is_file() and pdf.stat().st_size > 0
    listeners = subprocess.check_output(["ss", "-ltn"], text=True)
    bad = []
    for line in listeners.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        host = local.rsplit(":", 1)[0].strip("[]")
        if host in {"127.0.0.1", "::1", "0.0.0.0", "*", ""}:
            if host in {"0.0.0.0", "*"}:
                bad.append(local)
            continue
        try:
            packed = socket.inet_aton(host)
        except OSError:
            bad.append(local)
            continue
        if packed != socket.inet_aton("127.0.0.1"):
            bad.append(local)
    report["loopback_only"] = not bad
    report["listeners"] = listeners
except Exception as exc:
    report["error"] = str(exc)
Path("/workspace/outputs/browser-check.json").write_text(
    json.dumps(report) + "\n"
)
if report["error"]:
    raise SystemExit(report["error"])
PY
"""


def image_check_needs_sudo() -> bool:
    if os.geteuid() == 0:
        return False
    return os.environ.get(CHECK_SUDO_MARK) != "1"


def image_check_sudo_argv(extra: list[str]) -> list[str]:
    argv = [
        "sudo",
        "-E",
        "env",
        f"PATH={os.environ.get('PATH', '')}",
        f"HOME={os.environ.get('HOME', '')}",
        f"{CHECK_SUDO_MARK}=1",
    ]
    for key in ("XDG_CACHE_HOME", "XDG_DATA_HOME"):
        value = os.environ.get(key)
        if value:
            argv.append(f"{key}={value}")
    argv.extend([sys.executable, "-m", "apipi", "images", "check", *extra])
    return argv


def reexec_image_check(extra: list[str]) -> None:
    argv = image_check_sudo_argv(extra)
    print(CHECK_SUDO_NOTICE, file=sys.stderr)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise ConfigError(f"image check needs sudo: {exc}") from exc


def image_rootfs_path(settings: Settings, image_id: str, override: str | None) -> Path:
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"{image_id} rootfs not found: {path}")
        return path
    from apipi.worker.pi.microvm import microvm_images

    _kernel, rootfs = microvm_images(settings, image=image_id)
    return Path(rootfs)


def _run_rootfs_script(script: str, args: list[str]) -> None:
    argv = ["sh", "-c", script, "sh", *args]
    if os.geteuid() != 0:
        if shutil.which("sudo") is None:
            raise ConfigError("rootfs check needs root or sudo to mount the image")
        argv = ["sudo", "-E", *argv]
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        raise ConfigError("rootfs check failed")


def check_image_rootfs(image_id: str, rootfs: Path) -> None:
    if not rootfs.is_file():
        raise ConfigError(f"{image_id} rootfs not found: {rootfs}")
    _run_rootfs_script(_BASE_SCRIPT, [str(rootfs)])
    if image_id == "browser":
        _run_rootfs_script(
            _BROWSER_SCRIPT,
            [str(rootfs), PINNED_AGENT_BROWSER],
        )


def parse_check_tar(blob: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not blob:
        return None, None
    browser: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name.lstrip("./")
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                if name.endswith("browser-check.json"):
                    loaded = json.loads(extracted.read().decode())
                    if isinstance(loaded, dict):
                        browser = loaded
                elif name.endswith("image-check.json"):
                    loaded = json.loads(extracted.read().decode())
                    if isinstance(loaded, dict):
                        tools = loaded
    except tarfile.TarError:
        return None, None
    return browser, tools


async def check_browser_boot(settings: Settings) -> None:
    from apipi.worker.pi.microvm import spawn_microvm_pi

    workspace = Path(tempfile.mkdtemp(prefix="apipi-browser-check-"))
    (workspace / "outputs").mkdir()
    (workspace / ".apipi").mkdir()
    (workspace / ".apipi" / "browser-check.sh").write_text(_BOOT_SCRIPT)
    proc = None
    try:
        proc = await spawn_microvm_pi(
            settings,
            cwd=str(workspace),
            tools=True,
            mem_mib=settings.sandbox_l_mem_mib,
            image="browser",
            extra_env={"APIPI_IMAGE_CHECK": "1"},
        )
        errors: list[str] = []

        async def watch() -> None:
            async for event in proc._events():
                if event.get("type") == "extension_error":
                    errors.append(extension_error_text(event))

        task = asyncio.create_task(watch())
        deadline = time.monotonic() + BOOT_TIMEOUT_SEC
        browser: dict[str, Any] | None = None
        tools: dict[str, Any] | None = None
        try:
            while time.monotonic() < deadline:
                if errors:
                    raise ConfigError(errors[0])
                pull = proc.pull_artifacts
                if pull is None:
                    blob = b""
                else:
                    try:
                        blob = await asyncio.wait_for(pull(), timeout=5)
                    except (OSError, TimeoutError, ConfigError):
                        blob = b""
                found_browser, found_tools = parse_check_tar(blob)
                if found_browser is not None:
                    browser = found_browser
                if found_tools is not None:
                    tools = found_tools
                if browser is not None and tools is not None:
                    break
                await asyncio.sleep(1)
        finally:
            task.cancel()
        if errors:
            raise ConfigError(errors[0])
        if browser is None or tools is None:
            raise ConfigError("browser boot check timed out")
        raw_error = browser.get("error")
        if isinstance(raw_error, str) and raw_error:
            raise ConfigError(f"browser boot check failed: {raw_error}")
        if PINNED_AGENT_BROWSER not in str(browser.get("version", "")):
            raise ConfigError("browser boot check version does not match the pin")
        if not browser.get("snapshot"):
            raise ConfigError("browser boot check did not snapshot a local page")
        shot = str(browser.get("screenshot", ""))
        if not shot.startswith("/workspace/.browser"):
            raise ConfigError("browser boot check screenshot was not under .browser")
        if not browser.get("pdf"):
            raise ConfigError("browser boot check pdf failed")
        if not browser.get("loopback_only"):
            raise ConfigError("browser boot check found a non-loopback listener")
        names = tools.get("tools")
        listed = names if isinstance(names, list) else []
        if any(
            isinstance(name, str) and name.startswith("mcp_playwright_")
            for name in listed
        ):
            raise ConfigError("browser boot check still registered mcp_playwright_*")
        if not tools.get("skill"):
            raise ConfigError("browser boot check did not see the browser skill")
        print("browser boot check ok")
    finally:
        if proc is not None:
            await proc.terminate()
        shutil.rmtree(workspace, ignore_errors=True)


def run_image_check(
    settings: Settings, image_id: str, *, rootfs: str | None, boot: bool
) -> None:
    if boot and image_id != "browser":
        raise ConfigError("apipi images check --boot is only for the browser image")
    path = image_rootfs_path(settings, image_id, rootfs)
    check_image_rootfs(image_id, path)
    if boot:
        asyncio.run(check_browser_boot(settings))
