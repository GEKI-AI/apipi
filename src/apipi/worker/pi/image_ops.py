import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import zstandard

from apipi import __version__
from apipi.common.image_recipes import images_root, read_image_env, recipe_archs
from apipi.common.images import (
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
    load_manifest,
    manifest_name,
    recipe_sha256,
    sha256_file,
)
from apipi.config import ConfigError
from apipi.worker.pi.image_store import FileImageStore, HttpImageStore, S3ImageStore
from apipi.worker.pi.install import rootfs_build_args, rootfs_output_name
from apipi.worker.pi.version import (
    PINNED_AGENT_BROWSER,
    PINNED_CHROME_HEADLESS_SHELL,
    PINNED_DEBIAN_DIGEST,
    PINNED_NODE,
    PINNED_PI,
)

log = logging.getLogger("apipi.worker.pi")
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


def _compressor() -> zstandard.ZstdCompressor:
    from apipi.worker.pi.image_catalog import ZSTD_WINDOW_LOG

    params = zstandard.ZstdCompressionParameters.from_level(
        19, window_log=ZSTD_WINDOW_LOG
    )
    return zstandard.ZstdCompressor(compression_params=params)


def compress_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    compressor = _compressor()
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
    pi_version: str = PINNED_PI,
    min_apipi_version: str | None = None,
) -> ImageManifest:
    if not rootfs.is_file() or not kernel.is_file():
        raise ConfigError(f"image build did not write {rootfs} and {kernel}")
    env = read_image_env(images_root() / image_id / "image.env")
    guest = guest_sh_path()
    guest_digest = sha256_file(guest)
    recipe = recipe_sha256(images_root(), image_id)
    agent_browser = PINNED_AGENT_BROWSER if image_id == "browser" else ""
    chrome = PINNED_CHROME_HEADLESS_SHELL if image_id == "browser" else ""
    version = image_version(
        pi_version,
        PINNED_DEBIAN_DIGEST,
        PINNED_NODE,
        guest_digest,
        recipe,
        agent_browser=agent_browser,
        chrome=chrome,
    )
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
    raw_vcpus = env.get("MIN_VCPUS", "1") or "1"
    if not raw_vcpus.isdigit() or int(raw_vcpus) < 1:
        raise ConfigError(f"recipe {image_id} MIN_VCPUS must be an integer >= 1")
    min_vcpus = int(raw_vcpus)
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
        base=PINNED_DEBIAN_DIGEST,
        node_version=PINNED_NODE,
        pi_version=pi_version,
        guest_sh_sha256=guest_digest,
        recipe_sha256=recipe,
        min_apipi_version=min_apipi_version or __version__,
        min_size=min_size,
        min_vcpus=min_vcpus,
        agent_browser=agent_browser,
        chrome=chrome,
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
    allowed = recipe_archs(image_id)
    if machine not in allowed:
        raise ConfigError(f"image {image_id} is not built for {machine}")
    work = out_dir / ".work" / image_id
    work.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PINNED_PI"] = PINNED_PI
    env.pop("ALPINE_VER", None)
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
    )


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


def _manifest_newer(left: ImageManifest, right: ImageManifest) -> bool:
    if left.created_at != right.created_at:
        return left.created_at > right.created_at
    return left.version > right.version


def _newest_manifests(source: Path, ids: list[str]) -> list[ImageManifest]:
    newest: dict[tuple[str, str], ImageManifest] = {}
    for manifest in _built_manifests(source, ids):
        key = (manifest.id, manifest.arch)
        current = newest.get(key)
        if current is None or _manifest_newer(manifest, current):
            newest[key] = manifest
    return list(newest.values())


def publish_images(
    store: FileImageStore | S3ImageStore | HttpImageStore,
    source: Path,
    *,
    ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    store_version: str | None = None,
) -> list[str]:
    if not store_version:
        raise ConfigError("push requires --store-version")
    if force:
        raise ConfigError("push --force is refused for a versioned store prefix")
    return publish_versioned(
        store, source, version=store_version, ids=ids, dry_run=dry_run
    )


def publish_versioned(
    store: FileImageStore | S3ImageStore | HttpImageStore,
    source: Path,
    *,
    version: str,
    ids: list[str] | None = None,
    dry_run: bool = False,
    commit: str = "",
) -> list[str]:
    from apipi.common.images import (
        flat_artifact_name,
        flat_manifest_name,
        sha256_bytes,
    )
    from apipi.worker.pi.image_catalog import (
        checksum_lines,
        normalize_version,
        parse_checksums,
    )

    if store.exists("index.json") and store.exists("SHA256SUMS"):
        raise ConfigError(
            f"store version {normalize_version(version)} already has index.json"
        )
    manifests = _newest_manifests(source, ids or [])
    files: dict[str, bytes] = {}
    images: list[ImageIndexEntry] = []
    kernels: dict[str, KernelIndexEntry] = {}
    for manifest in manifests:
        built_name = manifest_name(manifest.id, manifest.version, manifest.arch)
        zst = source / manifest.rootfs.path
        man = source / built_name
        if not zst.is_file() or not man.is_file():
            raise ConfigError(f"build dir is missing {zst.name} or {man.name}")
        meta_path = source / f"vmlinux-{manifest.arch}.json"
        kernel = KernelIndexEntry.model_validate_json(meta_path.read_text())
        flat_zst = flat_artifact_name(manifest.id, manifest.arch)
        flat_man = flat_manifest_name(manifest.id, manifest.arch)
        body = load_manifest(man.read_text())
        body.rootfs.path = flat_zst
        body.kernel.path = kernel.path
        text = dump_manifest(body).encode()
        files[flat_zst] = zst.read_bytes()
        files[flat_man] = text
        kernel_blob = source / kernel.path
        if not kernel_blob.is_file():
            raise ConfigError(f"build dir is missing {kernel.path}")
        files[kernel.path] = kernel_blob.read_bytes()
        images.append(
            ImageIndexEntry(
                id=manifest.id,
                version=manifest.version,
                arch=manifest.arch,
                manifest=flat_man,
                manifest_sha256=sha256_bytes(text),
                kernel_version=kernel.version,
            )
        )
        kernels[manifest.arch] = kernel
    index = ImageIndex(
        schema_version=2,
        store_version=normalize_version(version),
        apipi_version=__version__,
        pi_version=manifests[0].pi_version if manifests else "",
        source_commit=commit,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        kernels=list(kernels.values()),
        images=images,
    )
    files["index.json"] = dump_index(index).encode()
    sums = checksum_lines(files)
    parse_checksums(sums)
    planned = [*sorted(files), "SHA256SUMS"]
    if dry_run:
        return planned
    for name, blob in files.items():
        if name == "index.json":
            continue
        store.put_bytes(name, blob)
    store.put_bytes("SHA256SUMS", sums.encode())
    store.put_bytes("index.json", files["index.json"])
    return planned
