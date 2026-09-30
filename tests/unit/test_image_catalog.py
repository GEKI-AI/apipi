from pathlib import Path

import pytest

from apipi.config import ConfigError, Settings
from apipi.worker.pi.image_catalog import (
    checksum_lines,
    join_store,
    normalize_version,
    parse_checksums,
    split_bytes,
    verify_checksums,
)
from apipi.worker.pi.image_ops import package_image, publish_images
from apipi.worker.pi.image_store import open_image_store
from apipi.worker.pi.images import load_index


def test_normalize_and_join() -> None:
    assert normalize_version("v0.12.0") == "0.12.0"
    assert join_store("file:///images", "0.12.0").endswith("/v0.12.0")


def test_checksums_round_trip() -> None:
    files = {"index.json": b"{}", "default-x86_64.ext4.zst": b"blob"}
    text = checksum_lines(files)
    assert parse_checksums(text)["index.json"]
    verify_checksums(files, text)
    bad = dict(files)
    bad["index.json"] = b"nope"
    with pytest.raises(ConfigError, match="sha256 mismatch"):
        verify_checksums(bad, text)


def test_split_bytes() -> None:
    parts = split_bytes(b"abcdefghij", 4)
    assert parts == [b"abcd", b"efgh", b"ij"]
    assert split_bytes(b"ab", 4) == [b"ab"]


def test_versioned_publish_is_flat(tmp_path: Path) -> None:
    work = tmp_path / "src" / "default"
    work.mkdir(parents=True)
    rootfs = work / "rootfs.ext4"
    kernel = work / "vmlinux"
    rootfs.write_bytes(b"rootfs-default")
    kernel.write_bytes(b"kernel-bytes")
    out = tmp_path / "out"
    package_image(
        image_id="default",
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
    )
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    store_dir = tmp_path / "store" / "v0.12.0"
    store = open_image_store(store_dir.as_uri(), settings, write=True)
    planned = publish_images(store, out, store_version="0.12.0")
    assert "default-x86_64.ext4.zst" in planned
    assert "SHA256SUMS" in planned
    index = load_index((store_dir / "index.json").read_text())
    assert index.schema_version == 2
    assert index.store_version == "0.12.0"
    assert index.images[0].latest is False
    assert index.images[0].manifest == "default-x86_64.manifest.json"
    with pytest.raises(ConfigError, match="already has"):
        publish_images(store, out, store_version="0.12.0")
