import io
import json
import tarfile

from apipi.config import Settings
from apipi.worker.pi.image_check import _BASE_SCRIPT, parse_check_tar
from apipi.worker.pi.microvm import guest_vcpus


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_rootfs_check_mounts_dev_before_mkdir() -> None:
    script = _BASE_SCRIPT
    dev_mount = script.index("mount -t devtmpfs devtmpfs")
    mkdir = script.index('mkdir -p "$mnt/dev/shm" "$mnt/dev/pts"')
    shm_mount = script.index(
        'mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs "$mnt/dev/shm"'
    )
    assert dev_mount < mkdir < shm_mount
    assert 'mkdir -p "$mnt/dev" "$mnt/dev/shm"' not in script


def test_parse_check_tar_reads_browser() -> None:
    browser = {"snapshot": True, "error": None}
    blob = _tar(
        {
            "outputs/browser-check.json": (json.dumps(browser) + "\n").encode(),
        }
    )
    assert parse_check_tar(blob) == browser


def test_parse_check_tar_empty() -> None:
    assert parse_check_tar(b"") is None


def test_guest_vcpus_l_defaults_to_two() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
    )
    assert guest_vcpus(settings, 512) == 1
    assert guest_vcpus(settings, 1024) == 1
    assert guest_vcpus(settings, 2048) == 2
    assert guest_vcpus(settings, 1024, image="browser") == 2
    assert settings.sandbox_vcpus("S") == 1
    assert settings.sandbox_vcpus("L") == 2
