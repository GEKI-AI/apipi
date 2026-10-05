import os
import shutil
import subprocess

import pytest

from apipi.config import ConfigError, Settings
from apipi.worker.pi.microvm import microvm_images, require_microvm


def image_paths() -> tuple[str | None, str | None]:
    from apipi.config import load_settings

    try:
        settings = load_settings()
    except Exception:
        return None, None
    try:
        return microvm_images(settings)
    except ConfigError:
        return None, None


def _tap_or_skip() -> None:
    ip_bin = shutil.which("ip")
    if ip_bin is None:
        pytest.skip("APIPI_RUN_MODE=microvm requires ip")
    name = f"apipit{os.getpid()}"
    added = subprocess.run(
        [ip_bin, "tuntap", "add", "dev", name, "mode", "tap"],
        capture_output=True,
    )
    subprocess.run([ip_bin, "link", "delete", "dev", name], capture_output=True)
    if added.returncode != 0:
        pytest.skip("APIPI_RUN_MODE=microvm requires TAP")


def microvm_or_skip(settings: Settings) -> None:
    try:
        require_microvm(settings)
    except ConfigError as exc:
        pytest.skip(str(exc))
    _tap_or_skip()
