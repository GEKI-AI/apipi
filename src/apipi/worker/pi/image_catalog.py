import hashlib
import logging
import re
from pathlib import Path

from apipi.config import ConfigError, Settings
from apipi.worker.pi.images import sha256_bytes

log = logging.getLogger("apipi.worker.pi")

OFFICIAL_IMAGE_BASE = "https://github.com/GEKI-AI/apipi/releases/download"
PART_LIMIT = 1536 * 1024 * 1024
ZSTD_WINDOW_LOG = 27
_VERSION_DIR = re.compile(r"^v[0-9]")
SIGSTORE_ISSUER = "https://token.actions.githubusercontent.com"
SIGSTORE_IDENTITY = (
    "https://github.com/GEKI-AI/apipi/.github/workflows/images.yml@refs/tags/v{version}"
)


def normalize_version(value: str) -> str:
    text = value.strip()
    if text.startswith("v") and len(text) > 1 and text[1].isdigit():
        return text[1:]
    return text


def version_prefix(version: str) -> str:
    return f"v{normalize_version(version)}"


def join_store(base: str, version: str) -> str:
    return f"{base.rstrip('/')}/{version_prefix(version)}"


def has_version_segment(uri: str) -> bool:
    path = uri.rstrip("/").rsplit("/", 1)[-1]
    return _VERSION_DIR.match(path) is not None


def checksum_lines(files: dict[str, bytes]) -> str:
    lines: list[str] = []
    for name in sorted(files):
        if name in {"SHA256SUMS", "SHA256SUMS.sigstore.json"}:
            continue
        lines.append(f"{sha256_bytes(files[name])}  {name}")
    return "\n".join(lines) + ("\n" if lines else "")


def parse_checksums(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest, _, name = stripped.partition("  ")
        if len(digest) != 64 or not name or ".." in Path(name).parts:
            raise ConfigError("SHA256SUMS has a bad line")
        found[name] = digest
    return found


def split_bytes(data: bytes, limit: int = PART_LIMIT) -> list[bytes]:
    if limit < 1:
        raise ConfigError("split limit must be at least 1")
    if len(data) <= limit:
        return [data]
    return [data[index : index + limit] for index in range(0, len(data), limit)]


def verify_checksums(files: dict[str, bytes], sums: str) -> None:
    expected = parse_checksums(sums)
    for name, digest in expected.items():
        blob = files.get(name)
        if blob is None:
            raise ConfigError(f"SHA256SUMS lists missing file {name}")
        actual = sha256_bytes(blob)
        if actual != digest:
            raise ConfigError(f"sha256 mismatch for {name}")
    for name in files:
        if name in {"SHA256SUMS", "SHA256SUMS.sigstore.json"}:
            continue
        if name not in expected:
            raise ConfigError(f"SHA256SUMS is missing {name}")


def verify_signature(
    sums: bytes,
    bundle: bytes,
    *,
    version: str,
    identity: str | None = None,
    issuer: str | None = None,
) -> None:
    import importlib

    try:
        verify_mod = importlib.import_module("sigstore.verify")
        policy_mod = importlib.import_module("sigstore.verify.policy")
        models_mod = importlib.import_module("sigstore.models")
    except ImportError as exc:
        raise ConfigError(
            "signature verify needs the images extra: uv sync --extra images"
        ) from exc
    expected = identity or SIGSTORE_IDENTITY.format(version=normalize_version(version))
    try:
        parsed = models_mod.Bundle.from_json(bundle)
        policy = policy_mod.Identity(
            identity=expected, issuer=issuer or SIGSTORE_ISSUER
        )
        verify_mod.Verifier.production().verify_artifact(sums, parsed, policy)
    except Exception as exc:
        raise ConfigError(f"image store signature failed: {exc}") from exc


def _from_uri(source: str, version: str | None) -> tuple[str, str]:
    text = source.strip()
    if "://" not in text:
        resolved = normalize_version(text)
        return join_store(OFFICIAL_IMAGE_BASE, resolved), resolved
    if has_version_segment(text):
        segment = text.rstrip("/").rsplit("/", 1)[-1]
        return text.rstrip("/"), normalize_version(segment)
    if not version:
        raise ConfigError("pass --version when --from is a store base")
    resolved = normalize_version(version)
    return join_store(text, resolved), resolved


def mirror_store(
    settings: Settings,
    source: str,
    dest: str,
    *,
    version: str | None = None,
    no_signature: bool = False,
    dry_run: bool = False,
) -> list[str]:
    from apipi.worker.pi.image_store import open_image_store
    from apipi.worker.pi.images import load_index, sha256_bytes

    src_uri, resolved = _from_uri(source, version)
    if dest.startswith("https://"):
        raise ConfigError("https:// image stores are read-only")
    dest_uri = dest if has_version_segment(dest) else join_store(dest, resolved)
    src = open_image_store(src_uri, settings, write=False)
    sums = src.get("SHA256SUMS")
    if not isinstance(sums, bytes):
        sums = bytes(sums)
    if not no_signature:
        bundle = src.get("SHA256SUMS.sigstore.json")
        if not isinstance(bundle, bytes):
            bundle = bytes(bundle)
        verify_signature(sums, bundle, version=resolved)
    expected = parse_checksums(sums.decode())
    index_bytes = src.get("index.json")
    if not isinstance(index_bytes, bytes):
        index_bytes = bytes(index_bytes)
    if expected.get("index.json") != sha256_bytes(index_bytes):
        raise ConfigError("sha256 mismatch for index.json")
    index = load_index(index_bytes)
    names = ["index.json", "SHA256SUMS"]
    if not no_signature:
        names.append("SHA256SUMS.sigstore.json")
    for item in index.images:
        names.append(item.manifest)
    for kernel in index.kernels:
        names.append(kernel.path)
    from apipi.worker.pi.images import load_manifest

    for item in index.images:
        manifest = load_manifest(src.get(item.manifest))
        if manifest.rootfs.parts:
            names.extend(part.path for part in manifest.rootfs.parts)
        else:
            names.append(manifest.rootfs.path)
    planned = list(dict.fromkeys(names))
    if dry_run:
        return planned
    target = open_image_store(dest_uri, settings, write=True)
    if target.exists("index.json") and target.exists("SHA256SUMS"):
        current = target.get("SHA256SUMS")
        if current != sums:
            raise ConfigError("refusing to overwrite a complete image store prefix")
    for name in planned:
        if name in {"index.json", "SHA256SUMS", "SHA256SUMS.sigstore.json"}:
            continue
        blob = src.get(name)
        if not isinstance(blob, bytes):
            blob = bytes(blob)
        digest = expected.get(name)
        if digest and sha256_bytes(blob) != digest:
            raise ConfigError(f"sha256 mismatch for {name}")
        if target.exists(name):
            continue
        target.put_bytes(name, blob)
    if not no_signature and src.exists("SHA256SUMS.sigstore.json"):
        bundle = src.get("SHA256SUMS.sigstore.json")
        if not isinstance(bundle, bytes):
            bundle = bytes(bundle)
        target.put_bytes("SHA256SUMS.sigstore.json", bundle)
    target.put_bytes("SHA256SUMS", sums)
    target.put_bytes("index.json", index_bytes)
    return planned


def verify_store(
    settings: Settings,
    *,
    source: str | None,
    version: str | None,
    local: bool,
    no_signature: bool,
) -> None:
    from apipi.worker.pi.image_pull import configured_images_dir
    from apipi.worker.pi.image_store import open_image_store
    from apipi.worker.pi.images import (
        load_index,
        load_manifest,
        sha256_bytes,
        sha256_file,
    )

    if local:
        root = configured_images_dir(settings)
        for folder in root.iterdir() if root.is_dir() else []:
            current = folder / "current"
            if not current.is_file():
                continue
            version_name = current.read_text().strip()
            manifest_path = folder / version_name / "manifest.json"
            rootfs = folder / version_name / "rootfs.ext4"
            manifest = load_manifest(manifest_path.read_text())
            if sha256_file(rootfs) != manifest.rootfs.sha256:
                raise ConfigError(f"sha256 mismatch for local image {folder.name}")
        return
    if not source:
        raise ConfigError("pass --source or --local")
    src_uri, resolved = _from_uri(source, version)
    store = open_image_store(src_uri, settings, write=False)
    sums = store.get("SHA256SUMS")
    if not isinstance(sums, bytes):
        sums = bytes(sums)
    if not no_signature:
        bundle = store.get("SHA256SUMS.sigstore.json")
        if not isinstance(bundle, bytes):
            bundle = bytes(bundle)
        verify_signature(sums, bundle, version=resolved)
    expected = parse_checksums(sums.decode())
    index_bytes = store.get("index.json")
    if not isinstance(index_bytes, bytes):
        index_bytes = bytes(index_bytes)
    if expected.get("index.json") != sha256_bytes(index_bytes):
        raise ConfigError("sha256 mismatch for index.json")
    index = load_index(index_bytes)
    for item in index.images:
        manifest_bytes = store.get(item.manifest)
        if not isinstance(manifest_bytes, bytes):
            manifest_bytes = bytes(manifest_bytes)
        digest = sha256_bytes(manifest_bytes)
        if item.manifest_sha256 and digest != item.manifest_sha256:
            raise ConfigError(f"sha256 mismatch for {item.manifest}")
        manifest = load_manifest(manifest_bytes)
        blob = store.get(manifest.rootfs.path)
        if not isinstance(blob, bytes):
            blob = bytes(blob)
        if sha256_bytes(blob) != manifest.rootfs.compressed_sha256:
            raise ConfigError(f"sha256 mismatch for {manifest.rootfs.path}")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
