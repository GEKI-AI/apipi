import os
import shutil
from pathlib import Path

import zstandard

from apipi import __version__
from apipi.config import ConfigError, Settings
from apipi.worker.pi.image_store import (
    FileImageStore,
    HttpImageStore,
    S3ImageStore,
    open_image_store,
)
from apipi.worker.pi.images import (
    ImageFormatError,
    ImageIndex,
    ImageManifest,
    LocalImage,
    latest_entry,
    load_index,
    load_manifest,
    local_images,
    local_kernel_path,
    read_current,
    require_compatible,
    sha256_file,
    write_current,
)
from apipi.worker.pi.version import PINNED_PI

Store = FileImageStore | S3ImageStore | HttpImageStore


def configured_images_dir(settings: Settings | None = None) -> Path:
    from apipi.worker.pi.microvm import xdg_cache_home

    if settings is not None and settings.images_dir:
        return Path(settings.images_dir)
    if settings is None:
        raw = os.environ.get("APIPI_IMAGES_DIR")
        if raw:
            return Path(raw)
    return xdg_cache_home() / "apipi" / "images"


def _host_arch() -> str:
    machine = os.uname().machine
    if machine not in {"x86_64", "aarch64"}:
        raise ConfigError(f"unsupported arch: {machine}")
    return machine


def _source(settings: Settings, source: str | None) -> str:
    uri = source or settings.image_source
    if not uri:
        raise ConfigError(
            "APIPI_IMAGE_SOURCE is unset. Set it, or pass --build to build locally."
        )
    return uri


def _download(store: Store, name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    getter = getattr(store, "get_to", None)
    tmp = dest.with_name(dest.name + ".part")
    if callable(getter):
        getter(name, tmp)
    else:
        tmp.write_bytes(store.get(name))
    tmp.replace(dest)


def decompress_zstd(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    decompressor = zstandard.ZstdDecompressor()
    with source.open("rb") as raw, tmp.open("wb") as out:
        decompressor.copy_stream(raw, out)
    tmp.replace(dest)


def _select_ids(index: ImageIndex, ids: list[str] | None, arch: str) -> list[str]:
    known = {item.id for item in index.images if item.arch == arch}
    if ids:
        missing = [item for item in ids if item not in known]
        if missing:
            raise ConfigError(f"unknown image {', '.join(missing)}")
        return list(ids)
    return sorted(known)


def _install_verified(
    images_dir: Path,
    manifest: ImageManifest,
    zst: Path,
) -> None:
    if sha256_file(zst) != manifest.rootfs.compressed_sha256:
        zst.unlink(missing_ok=True)
        raise ConfigError(f"compressed sha256 mismatch for {manifest.id}")
    version_dir = images_dir / manifest.id / manifest.version
    version_dir.mkdir(parents=True, exist_ok=True)
    rootfs = version_dir / "rootfs.ext4"
    try:
        decompress_zstd(zst, rootfs)
    except zstandard.ZstdError as exc:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise ConfigError(f"could not decompress {manifest.id}") from exc
    if sha256_file(rootfs) != manifest.rootfs.sha256:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise ConfigError(f"sha256 mismatch for {manifest.id}")
    (version_dir / "manifest.json").write_text(
        manifest.model_dump_json(by_alias=True, indent=2) + "\n"
    )
    write_current(images_dir, manifest.id, manifest.version)
    zst.unlink(missing_ok=True)


def pull_images(
    settings: Settings,
    *,
    ids: list[str] | None = None,
    source: str | None = None,
    force: bool = False,
    client: object | None = None,
) -> list[str]:
    uri = _source(settings, source)
    store = open_image_store(uri, settings, write=False, client=client)
    try:
        index = load_index(store.get("index.json"))
    except ImageFormatError as exc:
        raise ConfigError(str(exc)) from exc
    arch = _host_arch()
    chosen = ids
    if chosen is None and settings.sandbox_images:
        chosen = list(settings.sandbox_images)
    selected = _select_ids(index, chosen, arch)
    images_dir = configured_images_dir(settings)
    images_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for image_id in selected:
        entry = latest_entry(index, image_id, arch)
        try:
            manifest = load_manifest(store.get(entry.manifest))
        except ImageFormatError as exc:
            raise ConfigError(str(exc)) from exc
        try:
            require_compatible(
                manifest, pi_version=PINNED_PI, apipi_version=__version__
            )
        except ImageFormatError as exc:
            raise ConfigError(str(exc)) from exc
        current = read_current(images_dir, image_id)
        rootfs = images_dir / image_id / manifest.version / "rootfs.ext4"
        if (
            not force
            and current == manifest.version
            and rootfs.is_file()
            and sha256_file(rootfs) == manifest.rootfs.sha256
        ):
            installed.append(f"{image_id} {manifest.version} (present)")
            continue
        incoming = images_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        zst = incoming / manifest.rootfs.path
        _download(store, manifest.rootfs.path, zst)
        _install_verified(images_dir, manifest, zst)
        installed.append(f"{image_id} {manifest.version}")
    _pull_kernel(store, index, arch, images_dir, force=force)
    return installed


def _pull_kernel(
    store: Store,
    index: ImageIndex,
    arch: str,
    images_dir: Path,
    *,
    force: bool,
) -> None:
    kernels = [item for item in index.kernels if item.arch == arch]
    if not kernels:
        return
    kernel = kernels[-1]
    dest = local_kernel_path(images_dir, arch)
    meta = dest.with_name("vmlinux.json")
    cached = dest.is_file() and not force and meta.is_file()
    if cached and kernel.sha256 in meta.read_text():
        return
    incoming = images_dir / ".incoming" / kernel.path
    _download(store, kernel.path, incoming)
    if sha256_file(incoming) != kernel.compressed_sha256:
        incoming.unlink(missing_ok=True)
        raise ConfigError("compressed sha256 mismatch for vmlinux")
    dest.parent.mkdir(parents=True, exist_ok=True)
    decompress_zstd(incoming, dest)
    if sha256_file(dest) != kernel.sha256:
        dest.unlink(missing_ok=True)
        raise ConfigError("sha256 mismatch for vmlinux")
    meta.write_text(kernel.model_dump_json(indent=2) + "\n")
    incoming.unlink(missing_ok=True)


def list_images(
    settings: Settings,
    *,
    remote: bool = False,
    client: object | None = None,
) -> list[tuple[str, str, str, str]]:
    local = {item.id: item for item in local_images(configured_images_dir(settings))}
    remote_rows: dict[str, str] = {}
    if remote and settings.image_source:
        try:
            store = open_image_store(
                settings.image_source, settings, write=False, client=client
            )
            index = load_index(store.get("index.json"))
            arch = _host_arch()
            for item in index.images:
                if item.arch == arch and item.latest:
                    remote_rows[item.id] = item.version
        except (ConfigError, ImageFormatError, OSError):
            remote_rows = {}
    ids = sorted(set(local) | set(remote_rows))
    rows: list[tuple[str, str, str, str]] = []
    for image_id in ids:
        have = local.get(image_id)
        far = remote_rows.get(image_id)
        if have and far:
            status = "local+remote" if have.version == far else "outdated"
            version = have.version
        elif have:
            status = "local"
            version = have.version
        else:
            status = "remote"
            version = far or ""
        digest = have.digest[:12] if have else ""
        rows.append((image_id, version, digest, status))
    return rows


def available_images(settings: Settings) -> list[LocalImage]:
    from apipi.worker.pi.microvm import default_rootfs_browser_path, default_rootfs_path

    found = local_images(configured_images_dir(settings))
    ids = {item.id for item in found}
    legacy = (
        ("default", default_rootfs_path()),
        ("browser", default_rootfs_browser_path()),
    )
    for image_id, path in legacy:
        if image_id in ids or not path.is_file():
            continue
        found.append(
            LocalImage(
                id=image_id,
                version="legacy",
                arch=_host_arch(),
                digest="legacy",
                min_size="M" if image_id == "browser" else "S",
                rootfs=path,
                manifest_path=path,
            )
        )
    return found
