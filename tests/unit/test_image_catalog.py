from pathlib import Path

import pytest

from apipi.config import ConfigError, Settings
from apipi.worker.pi.image_catalog import (
    checksum_lines,
    join_store,
    normalize_version,
    parse_checksums,
    republish_store,
    signer_identity,
    split_bytes,
    verify_checksums,
    warn_signer_override,
)
from apipi.worker.pi.image_ops import package_image, publish_images
from apipi.worker.pi.image_store import open_image_store
from apipi.worker.pi.images import (
    dump_index,
    dump_manifest,
    load_index,
    load_manifest,
    sha256_bytes,
)
from apipi.worker.pi.version import (
    PINNED_NODE_SHA256_AARCH64,
    PINNED_NODE_SHA256_X86_64,
)


def test_node_pins_match_upstream_lines() -> None:
    assert PINNED_NODE_SHA256_X86_64 == (
        "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
    )
    assert PINNED_NODE_SHA256_AARCH64 == (
        "6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2"
    )
    assert "fef2b1575" not in PINNED_NODE_SHA256_AARCH64


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


def _stamp() -> dict[str, str]:
    return {
        "version": "0.85.1-aaaaaaaa",
        "base": "sha256:" + "a" * 64,
        "node_version": "v24.21.0",
        "pi_version": "0.85.1",
        "guest_sh_sha256": sha256_bytes(b"guest"),
        "recipe_sha256": sha256_bytes(b"recipe"),
        "agent_browser": "",
        "chrome": "",
    }


def _store(stamp: dict[str, str], image_id: str = "default") -> dict[str, bytes]:
    rootfs = b"rootfs-bytes"
    kernel = b"kernel-bytes"
    manifest = load_manifest(
        {
            "schema": 2,
            "id": image_id,
            "version": stamp["version"],
            "arch": "x86_64",
            "rootfs": {
                "path": f"{image_id}-x86_64.ext4.zst",
                "compression": "zstd",
                "sha256": sha256_bytes(rootfs),
                "compressed_sha256": sha256_bytes(b"zst"),
                "size": len(rootfs),
                "compressed_size": 3,
            },
            "kernel": {
                "version": "abc123abc123",
                "sha256": sha256_bytes(kernel),
                "path": "vmlinux-x86_64.zst",
            },
            "base": stamp["base"],
            "node_version": stamp["node_version"],
            "pi_version": stamp["pi_version"],
            "guest_sh_sha256": stamp["guest_sh_sha256"],
            "recipe_sha256": stamp["recipe_sha256"],
            "min_apipi_version": "0.12.0",
            "min_size": "S",
            "packages": [],
            "created_at": "2026-09-30T00:00:00Z",
            "agent_browser": stamp["agent_browser"],
            "chrome": stamp["chrome"],
        }
    )
    text = dump_manifest(manifest).encode()
    from apipi.worker.pi.images import ImageIndex, ImageIndexEntry, KernelIndexEntry

    index = ImageIndex(
        schema_version=2,
        store_version="0.12.0",
        apipi_version="0.12.0",
        kernels=[
            KernelIndexEntry(
                arch="x86_64",
                version="abc123abc123",
                path="vmlinux-x86_64.zst",
                sha256=sha256_bytes(kernel),
                compressed_sha256=sha256_bytes(b"k"),
            )
        ],
        images=[
            ImageIndexEntry(
                id=image_id,
                version=stamp["version"],
                arch="x86_64",
                manifest=f"{image_id}-x86_64.manifest.json",
                manifest_sha256=sha256_bytes(text),
                kernel_version="abc123abc123",
            )
        ],
    )
    return {
        "index.json": dump_index(index).encode(),
        f"{image_id}-x86_64.manifest.json": text,
        f"{image_id}-x86_64.ext4.zst": b"zst",
        "vmlinux-x86_64.zst": b"k",
    }


def test_republish_rewrites_the_release_and_keeps_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamp = _stamp()
    browser = dict(stamp)
    browser["agent_browser"] = "0.38.1"
    browser["chrome"] = "154.0.8037.92"

    def fake_stamp(image_id: str) -> dict[str, str]:
        return browser if image_id == "browser" else stamp

    monkeypatch.setattr("apipi.worker.pi.image_catalog.current_image_stamp", fake_stamp)
    files = _store(stamp)
    browser_files = _store(browser, "browser")
    index = load_index(files["index.json"])
    index.images.extend(load_index(browser_files["index.json"]).images)
    files.update(browser_files)
    files["index.json"] = dump_index(index).encode()
    published = republish_store(files, version="0.12.1", commit="abc")
    again = load_index(published["index.json"])
    assert again.store_version == "0.12.1"
    assert again.apipi_version
    assert published["default-x86_64.ext4.zst"] == b"zst"
    assert "SHA256SUMS.sigstore.json" not in published
    with pytest.raises(ConfigError, match="cannot be reused"):
        republish_store(_store(stamp), version="0.12.1", commit="abc")


def test_signer_override_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_verify_store(_settings: object, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setattr("apipi.worker.pi.image_catalog.verify_store", fake_verify_store)
    from apipi.cli import main

    assert (
        main(
            [
                "images",
                "verify",
                "--source",
                "file:///tmp/store",
                "--signer-identity",
                "custom-id",
                "--signer-issuer",
                "https://example",
            ]
        )
        == 0
    )
    assert seen["signer_identity"] == "custom-id"
    assert seen["signer_issuer"] == "https://example"
    with pytest.warns(UserWarning, match="signer identity"):
        warn_signer_override("0.12.0", "custom", None)
    assert signer_identity("v0.12.1").endswith("@refs/tags/v0.12.1")


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
