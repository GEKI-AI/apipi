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
from apipi.mcp.stdio import start_mcp_stdio_tools
from apipi.worker.pi.proc import extension_error_text
from apipi.worker.pi.sandbox import playwright_tool

CHECK_SUDO_MARK = "APIPI_IMAGE_CHECK_SUDO"
CHECK_SUDO_NOTICE = "Need root for TAP, NAT, and jailer. Re-running under sudo."
BOOT_TIMEOUT_SEC = 90

_ROOTFS_SCRIPT = r"""
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
chroot "$mnt" /usr/bin/env HOME=/tmp \
  /usr/bin/chromium-browser --headless --no-sandbox --dump-dom about:blank >/dev/null
chroot "$mnt" node \
  /opt/apipi/playwright-mcp/node_modules/@playwright/mcp/cli.js --help >/dev/null
echo "browser rootfs check ok"
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
        raise ConfigError(f"browser image check needs sudo: {exc}") from exc


def browser_rootfs_path(settings: Settings, override: str | None) -> Path:
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"browser rootfs not found: {path}")
        return path
    from apipi.worker.pi.microvm import microvm_images

    _kernel, rootfs = microvm_images(settings, image="browser")
    return Path(rootfs)


def check_browser_rootfs(rootfs: Path) -> None:
    if not rootfs.is_file():
        raise ConfigError(f"browser rootfs not found: {rootfs}")
    argv = ["sh", "-c", _ROOTFS_SCRIPT, "sh", str(rootfs)]
    if os.geteuid() != 0:
        if shutil.which("sudo") is None:
            raise ConfigError(
                "browser rootfs check needs root or sudo to mount the image"
            )
        argv = ["sudo", "-E", *argv]
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        raise ConfigError("browser rootfs check failed")


def parse_check_tar(blob: bytes) -> tuple[dict[str, Any] | None, bool]:
    if not blob:
        return None, False
    report: dict[str, Any] | None = None
    png = False
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name.lstrip("./")
                if name.endswith("mcp-check.json"):
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    loaded = json.loads(extracted.read().decode())
                    if isinstance(loaded, dict):
                        report = loaded
                if name.endswith(".png") and member.size > 0:
                    png = True
    except tarfile.TarError:
        return None, False
    return report, png


async def check_browser_boot(settings: Settings) -> None:
    from apipi.worker.pi.microvm import spawn_microvm_pi

    workspace = Path(tempfile.mkdtemp(prefix="apipi-browser-check-"))
    (workspace / "outputs").mkdir()
    proc = None
    try:
        stdio = await start_mcp_stdio_tools([playwright_tool(settings)], on_host=False)
        proc = await spawn_microvm_pi(
            settings,
            cwd=str(workspace),
            tools=True,
            mcp_stdio=stdio,
            mem_mib=settings.sandbox_l_mem_mib,
            image="browser",
            extra_env={"APIPI_MCP_CHECK": "1"},
        )
        errors: list[str] = []

        async def watch() -> None:
            async for event in proc._events():
                if event.get("type") == "extension_error":
                    errors.append(extension_error_text(event))

        task = asyncio.create_task(watch())
        deadline = time.monotonic() + BOOT_TIMEOUT_SEC
        report: dict[str, Any] | None = None
        png = False
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
                found, png = parse_check_tar(blob)
                if found is not None:
                    report = found
                    break
                await asyncio.sleep(1)
        finally:
            task.cancel()
        if errors:
            raise ConfigError(errors[0])
        if report is None:
            raise ConfigError(
                "browser boot check timed out waiting for Playwright tools"
            )
        raw_error = report.get("error")
        if isinstance(raw_error, str) and raw_error:
            raise ConfigError(f"browser boot check failed: {raw_error}")
        tools = report.get("tools")
        names = tools if isinstance(tools, list) else []
        if not any(
            isinstance(name, str) and name.startswith("mcp_playwright_")
            for name in names
        ):
            raise ConfigError(
                "browser boot check did not register mcp_playwright_* tools"
            )
        if not png:
            raise ConfigError(
                "browser boot check did not write a screenshot under outputs/"
            )
        print("browser boot check ok")
    finally:
        if proc is not None:
            await proc.terminate()
        shutil.rmtree(workspace, ignore_errors=True)


def run_browser_check(settings: Settings, *, rootfs: str | None, boot: bool) -> None:
    path = browser_rootfs_path(settings, rootfs)
    check_browser_rootfs(path)
    if boot:
        asyncio.run(check_browser_boot(settings))
