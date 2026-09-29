import io
import json
import tarfile

from apipi.config import Settings
from apipi.worker.pi.image_check import parse_check_tar
from apipi.worker.pi.microvm import guest_vcpus


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_parse_check_tar_reads_report_and_png() -> None:
    report = {"tools": ["mcp_playwright_browser_navigate"], "error": None}
    blob = _tar(
        {
            "outputs/mcp-check.json": (json.dumps(report) + "\n").encode(),
            "outputs/check.png": b"png",
        }
    )
    found, png = parse_check_tar(blob)
    assert found == report
    assert png is True


def test_parse_check_tar_empty() -> None:
    assert parse_check_tar(b"") == (None, False)


def test_guest_vcpus_l_defaults_to_two() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
    )
    assert guest_vcpus(settings, 512) == 1
    assert guest_vcpus(settings, 1024) == 1
    assert guest_vcpus(settings, 2048) == 2
    assert settings.sandbox_vcpus("S") == 1
    assert settings.sandbox_vcpus("L") == 2
