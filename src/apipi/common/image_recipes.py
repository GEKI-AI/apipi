"""Read the sandbox image recipes: `images/<id>/image.env`."""

from pathlib import Path

from apipi.config import ConfigError


def images_root() -> Path:
    package = Path(__file__).resolve().parents[1]
    packaged = package / "worker" / "pi" / "images"
    if (packaged / "build.sh").is_file():
        return packaged
    repo = package.parents[1] / "images"
    if (repo / "build.sh").is_file():
        return repo
    raise ConfigError("apipi install --microvm cannot find image recipes")


def read_image_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def recipe_env(image_id: str) -> dict[str, str]:
    try:
        return read_image_env(images_root() / image_id / "image.env")
    except (OSError, ConfigError):
        return {}


def recipe_archs(image_id: str) -> frozenset[str]:
    raw = recipe_env(image_id).get("ARCHS", "")
    parts = [part for part in raw.split() if part]
    if not parts:
        return frozenset({"x86_64", "aarch64"})
    return frozenset(parts)


def recipe_ids() -> list[str]:
    root = images_root()
    found: list[str] = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "image.env").is_file():
            found.append(path.name)
    return found
