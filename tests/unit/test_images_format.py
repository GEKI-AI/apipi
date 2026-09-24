import json
from pathlib import Path

import pytest

from apipi.worker.pi.images import (
    ImageFormatError,
    ImageIndex,
    dump_index,
    dump_manifest,
    hash_files,
    image_version,
    load_index,
    load_manifest,
    local_images,
    recipe_sha256,
    require_compatible,
    sha256_bytes,
    write_current,
)


def _manifest(**overrides: object) -> dict[str, object]:
    guest = sha256_bytes(b"guest")
    recipe = sha256_bytes(b"recipe")
    body: dict[str, object] = {
        "schema": 1,
        "id": "browser",
        "version": image_version("0.85.1", "3.21.3", guest, recipe),
        "arch": "x86_64",
        "rootfs": {
            "path": "browser-0.85.1-aaaaaaaa-x86_64.ext4.zst",
            "compression": "zstd",
            "sha256": sha256_bytes(b"ext4"),
            "compressed_sha256": sha256_bytes(b"zst"),
            "size": 4096,
            "compressed_size": 100,
        },
        "kernel": {"version": "abc123abc123", "sha256": sha256_bytes(b"vmlinux")},
        "alpine_version": "3.21.3",
        "pi_version": "0.85.1",
        "guest_sh_sha256": guest,
        "recipe_sha256": recipe,
        "min_apipi_version": "0.4.0",
        "min_size": "M",
        "packages": ["chromium"],
        "created_at": "2026-09-24T00:00:00Z",
    }
    body.update(overrides)
    return body


def test_manifest_round_trip() -> None:
    loaded = load_manifest(_manifest())
    again = load_manifest(dump_manifest(loaded))
    assert again.id == "browser"
    assert again.min_size == "M"
    assert again.schema_version == 1
    assert again.rootfs.sha256 == sha256_bytes(b"ext4")


def test_unknown_schema_fails() -> None:
    raw = _manifest()
    raw["schema"] = 2
    with pytest.raises(ImageFormatError, match="unknown image manifest schema 2"):
        load_manifest(raw)


def test_bad_sha256_fails() -> None:
    raw = _manifest()
    rootfs = raw["rootfs"]
    assert isinstance(rootfs, dict)
    rootfs["sha256"] = "ZZ"
    with pytest.raises(ImageFormatError, match="sha256"):
        load_manifest(raw)


def test_version_changes_when_inputs_change() -> None:
    guest = sha256_bytes(b"guest")
    recipe = sha256_bytes(b"recipe")
    first = image_version("0.85.1", "3.21.3", guest, recipe)
    changed_guest = image_version("0.85.1", "3.21.3", sha256_bytes(b"other"), recipe)
    changed_recipe = image_version("0.85.1", "3.21.3", guest, sha256_bytes(b"other"))
    changed_alpine = image_version("0.85.1", "3.22.0", guest, recipe)
    assert first != changed_guest
    assert first != changed_recipe
    assert first != changed_alpine
    assert first.startswith("0.85.1-")
    assert len(first.split("-", 1)[1]) == 8


def test_recipe_hash_changes_when_file_changes(tmp_path: Path) -> None:
    images = tmp_path / "images"
    recipe = images / "default"
    recipe.mkdir(parents=True)
    (images / "build.sh").write_text("echo build\n")
    (recipe / "image.env").write_text("IMAGE_ID=default\n")
    first = recipe_sha256(images, "default")
    (recipe / "image.env").write_text("IMAGE_ID=default\nSIZE_MIB=2048\n")
    assert recipe_sha256(images, "default") != first
    (images / "build.sh").write_text("echo changed\n")
    assert recipe_sha256(images, "default") != first


def test_hash_files_is_order_independent() -> None:
    left = hash_files([("b", b"2"), ("a", b"1")])
    right = hash_files([("a", b"1"), ("b", b"2")])
    assert left == right


def test_min_apipi_version_check() -> None:
    manifest = load_manifest(_manifest())
    require_compatible(manifest, pi_version="0.85.1", apipi_version="0.4.0")
    require_compatible(manifest, pi_version="0.85.1", apipi_version="0.5.0")
    with pytest.raises(ImageFormatError, match="needs ApiPi"):
        require_compatible(manifest, pi_version="0.85.1", apipi_version="0.3.9")
    with pytest.raises(ImageFormatError, match="built for Pi"):
        require_compatible(manifest, pi_version="0.85.0", apipi_version="0.4.0")


def test_index_requires_one_latest() -> None:
    entry = {
        "id": "default",
        "version": "0.85.1-aaaaaaaa",
        "arch": "x86_64",
        "manifest": "default-0.85.1-aaaaaaaa-x86_64.json",
        "latest": True,
    }
    index = load_index({"schema": 1, "kernels": [], "images": [entry]})
    assert index.images[0].latest
    bad = {"schema": 1, "kernels": [], "images": [entry, {**entry, "latest": True}]}
    with pytest.raises(ImageFormatError, match="exactly one latest"):
        load_index(bad)
    dumped = json.loads(dump_index(index))
    assert dumped["schema"] == 1


def test_local_layout(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest())
    folder = tmp_path / manifest.id / manifest.version
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(dump_manifest(manifest))
    (folder / "rootfs.ext4").write_bytes(b"ext4")
    write_current(tmp_path, manifest.id, manifest.version)
    found = local_images(tmp_path)
    assert len(found) == 1
    assert found[0].id == "browser"
    assert found[0].digest == manifest.rootfs.sha256
    assert found[0].min_size == "M"
    assert (tmp_path / "browser" / "current").read_text().strip() == manifest.version


def test_empty_index_round_trip() -> None:
    index = ImageIndex(schema_version=1, kernels=[], images=[])
    assert load_index(dump_index(index)).images == []
