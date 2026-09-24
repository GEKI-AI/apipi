import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import zstandard

from apipi import __version__
from apipi.config import ConfigError
from apipi.worker.pi.image_store import FileImageStore, HttpImageStore, S3ImageStore
from apipi.worker.pi.images import (
    ImageFormatError,
    ImageIndex,
    ImageIndexEntry,
    ImageManifest,
    KernelIndexEntry,
    KernelRef,
    RootfsArtifact,
    artifact_name,
    dump_index,
    dump_manifest,
    image_version,
    kernel_artifact_name,
    kernel_version,
    load_index,
    load_manifest,
    manifest_name,
    recipe_sha256,
    sha256_file,
)
from apipi.worker.pi.install import (
    images_root,
    read_image_env,
    rootfs_build_args,
    rootfs_output_name,
)
from apipi.worker.pi.version import PINNED_PI

ALPINE_DEFAULT = "3.21.3"
HOST_ARCHS = frozenset({"x86_64", "aarch64"})


def host_arch(requested: str | None = None) -> str:
    machine = os.uname().machine
    if requested is not None and requested != machine:
        raise ConfigError(f"cross-build is not supported; this host is {machine}")
    if machine not in HOST_ARCHS:
        raise ConfigError(f"unsupported arch: {machine}")
    return machine


def _arch(value: str) -> Literal["x86_64", "aarch64"]:
    if value == "x86_64":
        return "x86_64"
    if value == "aarch64":
        return "aarch64"
    raise ConfigError(f"unsupported arch: {value}")


def _min_size(value: str, image_id: str) -> Literal["S", "M", "L"]:
    if value == "S":
        return "S"
    if value == "M":
        return "M"
    if value == "L":
        return "L"
    raise ConfigError(f"recipe {image_id} MIN_SIZE must be S, M, or L")


def guest_sh_path() -> Path:
    root = images_root()
    packaged = root.parent / "guest.sh"
    if packaged.is_file():
        return packaged
    checkout = root.parent / "src" / "apipi" / "worker" / "pi" / "guest.sh"
    if checkout.is_file():
        return checkout
    raise ConfigError("could not find guest.sh for the image build")


def compress_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=19)
    with (
        source.open("rb") as raw,
        dest.open("wb") as out,
        compressor.stream_writer(out) as writer,
    ):
        while True:
            chunk = raw.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)


def package_image(
    *,
    image_id: str,
    rootfs: Path,
    kernel: Path,
    out_dir: Path,
    arch: str,
    alpine_version: str = ALPINE_DEFAULT,
    pi_version: str = PINNED_PI,
    min_apipi_version: str | None = None,
) -> ImageManifest:
    if not rootfs.is_file() or not kernel.is_file():
        raise ConfigError(f"image build did not write {rootfs} and {kernel}")
    env = read_image_env(images_root() / image_id / "image.env")
    guest = guest_sh_path()
    guest_digest = sha256_file(guest)
    recipe = recipe_sha256(images_root(), image_id)
    version = image_version(pi_version, alpine_version, guest_digest, recipe)
    rootfs_digest = sha256_file(rootfs)
    kernel_digest = sha256_file(kernel)
    zst_name = artifact_name(image_id, version, arch)
    zst_path = out_dir / zst_name
    compress_file(rootfs, zst_path)
    kernel_name = kernel_artifact_name(arch)
    kernel_zst = out_dir / kernel_name
    compress_file(kernel, kernel_zst)
    packages = [part for part in env.get("PACKAGES", "").split() if part]
    min_size = _min_size(env.get("MIN_SIZE", "S"), image_id)
    machine = _arch(arch)
    manifest = ImageManifest(
        id=image_id,
        version=version,
        arch=machine,
        rootfs=RootfsArtifact(
            path=zst_name,
            compression="zstd",
            sha256=rootfs_digest,
            compressed_sha256=sha256_file(zst_path),
            size=rootfs.stat().st_size,
            compressed_size=zst_path.stat().st_size,
        ),
        kernel=KernelRef(version=kernel_version(kernel_digest), sha256=kernel_digest),
        alpine_version=alpine_version,
        pi_version=pi_version,
        guest_sh_sha256=guest_digest,
        recipe_sha256=recipe,
        min_apipi_version=min_apipi_version or __version__,
        min_size=min_size,
        packages=packages,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    manifest_path = out_dir / manifest_name(image_id, version, arch)
    manifest_path.write_text(dump_manifest(manifest))
    kernel_meta = KernelIndexEntry(
        arch=machine,
        version=manifest.kernel.version,
        path=kernel_name,
        sha256=kernel_digest,
        compressed_sha256=sha256_file(kernel_zst),
    )
    (out_dir / f"vmlinux-{arch}.json").write_text(
        kernel_meta.model_dump_json(indent=2) + "\n"
    )
    return manifest


def build_image(
    image_id: str,
    *,
    out_dir: Path,
    arch: str | None = None,
) -> ImageManifest:
    machine = host_arch(arch)
    work = out_dir / ".work" / image_id
    work.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PINNED_PI"] = PINNED_PI
    alpine = env.get("ALPINE_VER", ALPINE_DEFAULT)
    try:
        subprocess.run(rootfs_build_args(image_id, work), check=True, env=env)
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"could not build image {image_id}") from exc
    return package_image(
        image_id=image_id,
        rootfs=work / rootfs_output_name(image_id),
        kernel=work / "vmlinux",
        out_dir=out_dir,
        arch=machine,
        alpine_version=alpine,
    )


def _load_store_index(
    store: FileImageStore | S3ImageStore | HttpImageStore,
) -> ImageIndex:
    if not store.exists("index.json"):
        return ImageIndex(schema_version=1, kernels=[], images=[])
    try:
        return load_index(store.get("index.json"))
    except ImageFormatError as exc:
        raise ConfigError(str(exc)) from exc


def _built_manifests(source: Path, ids: list[str]) -> list[ImageManifest]:
    found: list[ImageManifest] = []
    for path in sorted(source.glob("*.json")):
        if path.name.startswith("vmlinux-"):
            continue
        try:
            manifest = load_manifest(path.read_text())
        except ImageFormatError:
            continue
        if ids and manifest.id not in ids:
            continue
        found.append(manifest)
    if ids:
        missing = [item for item in ids if item not in {row.id for row in found}]
        if missing:
            raise ConfigError(f"build dir has no manifest for {', '.join(missing)}")
    if not found:
        raise ConfigError(f"no image manifests in {source}")
    return found


def publish_images(
    store: FileImageStore | S3ImageStore | HttpImageStore,
    source: Path,
    *,
    ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[str]:
    wanted = ids or []
    manifests = _built_manifests(source, wanted)
    index = _load_store_index(store)
    planned: list[str] = []
    for manifest in manifests:
        name = manifest_name(manifest.id, manifest.version, manifest.arch)
        exists = any(
            item.id == manifest.id
            and item.version == manifest.version
            and item.arch == manifest.arch
            for item in index.images
        )
        if exists and not force:
            raise ConfigError(
                f"image {manifest.id} {manifest.version} {manifest.arch} "
                "is already published; pass --force to replace it"
            )
        zst = source / manifest.rootfs.path
        man = source / name
        if not zst.is_file() or not man.is_file():
            raise ConfigError(f"build dir is missing {zst.name} or {man.name}")
        planned.extend([zst.name, man.name])
        if not dry_run:
            store.put_file(zst.name, zst)
            store.put_file(man.name, man)
        for item in index.images:
            if item.id == manifest.id and item.arch == manifest.arch:
                item.latest = False
        index.images = [
            item
            for item in index.images
            if not (
                item.id == manifest.id
                and item.version == manifest.version
                and item.arch == manifest.arch
            )
        ]
        index.images.append(
            ImageIndexEntry(
                id=manifest.id,
                version=manifest.version,
                arch=manifest.arch,
                manifest=name,
                latest=True,
            )
        )
    archs = {item.arch for item in manifests}
    for arch in sorted(archs):
        meta_path = source / f"vmlinux-{arch}.json"
        if not meta_path.is_file():
            raise ConfigError(f"build dir is missing {meta_path.name}")
        kernel = KernelIndexEntry.model_validate_json(meta_path.read_text())
        blob = source / kernel.path
        if not blob.is_file():
            raise ConfigError(f"build dir is missing {kernel.path}")
        already = any(
            item.arch == kernel.arch and item.version == kernel.version
            for item in index.kernels
        )
        if not already or force:
            planned.append(kernel.path)
            if not dry_run:
                store.put_file(kernel.path, blob)
            index.kernels = [
                item
                for item in index.kernels
                if not (item.arch == kernel.arch and item.version == kernel.version)
            ]
            index.kernels.append(kernel)
    planned.append("index.json")
    if not dry_run:
        store.put_bytes("index.json", dump_index(index).encode())
    return planned
