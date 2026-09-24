import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SCHEMA = 1
IMAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ImageFormatError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _sha256(value: str) -> str:
    if not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None:
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return value


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise ValueError("path must be relative")
    if ".." in Path(value).parts or "\\" in value:
        raise ValueError("path must be relative")
    return value


def _version_parts(value: str) -> tuple[int, ...]:
    core = value.split("+", 1)[0].split("-", 1)[0]
    if not core or not all(part.isdigit() for part in core.split(".")):
        raise ImageFormatError(f"bad version {value}")
    return tuple(int(part) for part in core.split("."))


def version_less_equal(left: str, right: str) -> bool:
    first = _version_parts(left)
    second = _version_parts(right)
    width = max(len(first), len(second))
    first = first + (0,) * (width - len(first))
    second = second + (0,) * (width - len(second))
    return first <= second


class RootfsArtifact(_Model):
    path: str
    compression: Literal["zstd"]
    sha256: str
    compressed_sha256: str
    size: int
    compressed_size: int

    @field_validator("path")
    @classmethod
    def path_relative(cls, value: str) -> str:
        return _relative(value)

    @field_validator("sha256", "compressed_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("size", "compressed_size")
    @classmethod
    def non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("size must be >= 0")
        return value


class KernelRef(_Model):
    version: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha256(value)


class ImageManifest(_Model):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    id: str
    version: str
    arch: Literal["x86_64", "aarch64"]
    rootfs: RootfsArtifact
    kernel: KernelRef
    alpine_version: str
    pi_version: str
    guest_sh_sha256: str
    recipe_sha256: str
    min_apipi_version: str
    min_size: Literal["S", "M", "L"]
    packages: list[str]
    created_at: str

    @field_validator("id")
    @classmethod
    def image_id(cls, value: str) -> str:
        if IMAGE_ID.fullmatch(value) is None:
            raise ValueError("image id must match ^[a-z0-9][a-z0-9-]{0,31}$")
        return value

    @field_validator("guest_sh_sha256", "recipe_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha256(value)


class KernelIndexEntry(_Model):
    arch: Literal["x86_64", "aarch64"]
    version: str
    path: str
    sha256: str
    compressed_sha256: str

    @field_validator("path")
    @classmethod
    def path_relative(cls, value: str) -> str:
        return _relative(value)

    @field_validator("sha256", "compressed_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha256(value)


class ImageIndexEntry(_Model):
    id: str
    version: str
    arch: Literal["x86_64", "aarch64"]
    manifest: str
    latest: bool = False

    @field_validator("id")
    @classmethod
    def image_id(cls, value: str) -> str:
        if IMAGE_ID.fullmatch(value) is None:
            raise ValueError("image id must match ^[a-z0-9][a-z0-9-]{0,31}$")
        return value

    @field_validator("manifest")
    @classmethod
    def path_relative(cls, value: str) -> str:
        return _relative(value)


class ImageIndex(_Model):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    kernels: list[KernelIndexEntry]
    images: list[ImageIndexEntry]


class LocalImage:
    def __init__(
        self,
        *,
        id: str,
        version: str,
        arch: str,
        digest: str,
        min_size: str,
        rootfs: Path,
        manifest_path: Path,
    ) -> None:
        self.id = id
        self.version = version
        self.arch = arch
        self.digest = digest
        self.min_size = min_size
        self.rootfs = rootfs
        self.manifest_path = manifest_path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def kernel_version(kernel_sha256: str) -> str:
    digest = _sha256(kernel_sha256)
    return digest[:12]


def version_suffix(
    alpine_version: str, guest_sh_sha256: str, recipe_sha256: str
) -> str:
    payload = f"{alpine_version}\n{guest_sh_sha256}\n{recipe_sha256}\n".encode()
    return sha256_bytes(payload)[:8]


def image_version(
    pi_version: str,
    alpine_version: str,
    guest_sh_sha256: str,
    recipe_sha256: str,
) -> str:
    suffix = version_suffix(alpine_version, guest_sh_sha256, recipe_sha256)
    return f"{pi_version}-{suffix}"


def hash_files(files: list[tuple[str, bytes]]) -> str:
    hasher = hashlib.sha256()
    for path, content in sorted(files):
        hasher.update(path.encode())
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    return hasher.hexdigest()


def recipe_sha256(images_dir: Path, image_id: str) -> str:
    root = images_dir
    build = root / "build.sh"
    recipe = root / image_id
    if not build.is_file() or not recipe.is_dir():
        raise ImageFormatError(f"missing recipe {image_id}")
    files: list[tuple[str, bytes]] = [
        ("build.sh", build.read_bytes()),
    ]
    for path in sorted(recipe.iterdir()):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            files.append((rel, path.read_bytes()))
    return hash_files(files)


def artifact_name(image_id: str, version: str, arch: str) -> str:
    return f"{image_id}-{version}-{arch}.ext4.zst"


def manifest_name(image_id: str, version: str, arch: str) -> str:
    return f"{image_id}-{version}-{arch}.json"


def kernel_artifact_name(arch: str) -> str:
    return f"vmlinux-{arch}.zst"


def _as_object(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImageFormatError("image document is not JSON") from exc
    if not isinstance(loaded, dict):
        raise ImageFormatError("image document must be a JSON object")
    return loaded


def _schema(data: dict[str, Any], kind: str) -> None:
    if data.get("schema") != SCHEMA:
        raise ImageFormatError(f"unknown {kind} schema {data.get('schema')}")


def _invalid(kind: str, exc: ValidationError) -> ImageFormatError:
    return ImageFormatError(f"invalid {kind}: {exc.errors()[0]['msg']}")


def load_manifest(raw: str | bytes | dict[str, Any]) -> ImageManifest:
    data = _as_object(raw)
    _schema(data, "image manifest")
    try:
        return ImageManifest.model_validate(data)
    except ValidationError as exc:
        raise _invalid("image manifest", exc) from exc


def dump_manifest(manifest: ImageManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json", by_alias=True), indent=2) + "\n"


def _require_one_latest(index: ImageIndex) -> None:
    groups: dict[tuple[str, str], int] = {}
    for item in index.images:
        key = (item.id, item.arch)
        groups[key] = groups.get(key, 0) + int(item.latest)
    for (image_id, arch), count in groups.items():
        if count != 1:
            raise ImageFormatError(
                f"index needs exactly one latest {image_id} {arch}, found {count}"
            )


def load_index(raw: str | bytes | dict[str, Any]) -> ImageIndex:
    data = _as_object(raw)
    _schema(data, "image index")
    try:
        index = ImageIndex.model_validate(data)
    except ValidationError as exc:
        raise _invalid("image index", exc) from exc
    _require_one_latest(index)
    return index


def dump_index(index: ImageIndex) -> str:
    _require_one_latest(index)
    return json.dumps(index.model_dump(mode="json", by_alias=True), indent=2) + "\n"


def require_compatible(
    manifest: ImageManifest, *, pi_version: str, apipi_version: str
) -> None:
    if manifest.pi_version != pi_version:
        raise ImageFormatError(
            f"image {manifest.id} {manifest.version} was built for Pi "
            f"{manifest.pi_version}, this ApiPi pins {pi_version}"
        )
    if not version_less_equal(manifest.min_apipi_version, apipi_version):
        raise ImageFormatError(
            f"image {manifest.id} {manifest.version} needs ApiPi "
            f"{manifest.min_apipi_version} or newer"
        )


def latest_entry(index: ImageIndex, image_id: str, arch: str) -> ImageIndexEntry:
    matches = [
        item
        for item in index.images
        if item.id == image_id and item.arch == arch and item.latest
    ]
    if len(matches) != 1:
        raise ImageFormatError(f"no latest {image_id} for {arch}")
    return matches[0]


def write_current(images_dir: Path, image_id: str, version: str) -> None:
    folder = images_dir / image_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "current"
    tmp = folder / f".current.{version}.tmp"
    tmp.write_text(version + "\n")
    tmp.replace(target)


def read_current(images_dir: Path, image_id: str) -> str | None:
    path = images_dir / image_id / "current"
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return text or None


def local_images(images_dir: Path) -> list[LocalImage]:
    if not images_dir.is_dir():
        return []
    found: list[LocalImage] = []
    for path in sorted(images_dir.iterdir()):
        if not path.is_dir() or path.name == "kernels":
            continue
        version = read_current(images_dir, path.name)
        if version is None:
            continue
        manifest_path = path / version / "manifest.json"
        rootfs = path / version / "rootfs.ext4"
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path.read_text())
        found.append(
            LocalImage(
                id=manifest.id,
                version=manifest.version,
                arch=manifest.arch,
                digest=manifest.rootfs.sha256,
                min_size=manifest.min_size,
                rootfs=rootfs,
                manifest_path=manifest_path,
            )
        )
    return found


def local_kernel_path(images_dir: Path, arch: str) -> Path:
    return images_dir / "kernels" / arch / "vmlinux"
