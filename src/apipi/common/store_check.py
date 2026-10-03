"""The shared-filesystem proof between the API and a worker."""

import secrets
from pathlib import Path

from apipi.config import ConfigError

SHARED_STORE_ERROR = (
    "filesystem store requires a shared path: mount the same "
    "APIPI_LOCAL_STORE_DIR on the API and every worker"
)


def write_store_check(root: Path) -> tuple[str, str]:
    """Write a nonce marker file; the worker must read it back."""
    nonce = secrets.token_hex(16)
    marker = f".apipi-store-check-{nonce}"
    path = root / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(nonce, encoding="utf-8")
    return marker, nonce


def read_store_check(root: Path, marker: str, nonce: str) -> bool:
    """True when `marker` under `root` contains exactly `nonce`."""
    name = (marker or "").strip().strip("/")
    if not name or "/" in name or name in {".", ".."}:
        return False
    if not name.startswith(".apipi-store-check-"):
        return False
    try:
        text = (root / name).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(nonce) and text == nonce


def verify_store_proof(root: Path, marker: str, nonce: str) -> None:
    if not read_store_check(root, marker, nonce):
        raise ConfigError(SHARED_STORE_ERROR)
