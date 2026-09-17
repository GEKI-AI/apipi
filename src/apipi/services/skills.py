import io
import re
import shutil
import zipfile
from pathlib import Path

from apipi.env.setup import SetupError

_WELL_KNOWN = (".agents/skills", ".pi/skills")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_SKILL_FILES = 500


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def copy_capability_directories(workspace: Path, directories: list[str]) -> None:
    root = workspace.resolve()
    for index, raw in enumerate(directories):
        src = Path(raw)
        if not src.is_absolute() or not src.is_dir():
            continue
        if _is_under(src, root):
            continue
        dest = root / src.name
        if dest.exists():
            dest = root / f"{src.name}-{index}"
        shutil.copytree(src, dest)


def _capability_roots(workspace: Path, directories: list[str]) -> list[Path]:
    roots: list[Path] = []
    for index, raw in enumerate(directories):
        src = Path(raw)
        if not src.is_absolute():
            path = workspace / src
        elif _is_under(src, workspace):
            path = src
        else:
            path = workspace / src.name
            if not path.is_dir():
                path = workspace / f"{src.name}-{index}"
        if path.is_dir():
            roots.append(path)
    return roots


def _skill_files(root: Path) -> list[Path]:
    direct = root / "SKILL.md"
    if direct.is_file():
        return [direct]
    found: list[Path] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return found
    for child in children:
        skill = child / "SKILL.md"
        if child.is_dir() and skill.is_file():
            found.append(skill)
    return found


def discover_skill_dirs(
    workspace: Path | None, capability_directories: list[str] | None = None
) -> list[str]:
    if workspace is None or not workspace.is_dir():
        return []
    roots: list[Path] = []
    seen_roots: set[Path] = set()

    def add_root(path: Path) -> None:
        if not path.is_dir():
            return
        resolved = path.resolve()
        if resolved in seen_roots:
            return
        seen_roots.add(resolved)
        roots.append(path)

    if capability_directories:
        for path in _capability_roots(workspace, capability_directories):
            add_root(path)
    for rel in _WELL_KNOWN:
        add_root(workspace / rel)
    trees: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        for skill_md in _skill_files(root):
            tree = skill_md.parent.resolve()
            if tree in seen:
                continue
            seen.add(tree)
            trees.append(str(tree))
    return trees


def _skill_name_from_markdown(text: str) -> str | None:
    if not text.lstrip().startswith("---"):
        return None
    rest = text.lstrip()[3:].lstrip("\n")
    end = rest.find("\n---")
    if end < 0:
        return None
    for line in rest[:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "name":
            name = value.strip().strip("\"'")
            if name:
                return name
    return None


def _safe_zip_parts(name: str) -> list[str] | None:
    text = name.replace("\\", "/").strip("/")
    if not text:
        return []
    parts = [part for part in text.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return None
    return parts


def inspect_skill_zip(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SetupError("skills need a zip") from exc
    manifests: list[tuple[str, list[str]]] = []
    for info in archive.infolist():
        parts = _safe_zip_parts(info.filename)
        if parts is None:
            raise SetupError("skill zip path must stay inside the package")
        if not parts or info.is_dir():
            continue
        if parts[-1].lower() == "skill.md":
            manifests.append((info.filename, parts))
    if len(manifests) != 1:
        raise SetupError("skill zip needs exactly one SKILL.md")
    manifest, manifest_parts = manifests[0]
    try:
        text = archive.read(manifest).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetupError("SKILL.md must be utf-8") from exc
    folder = manifest_parts[0] if len(manifest_parts) > 1 else "skill"
    raw = _skill_name_from_markdown(text) or folder
    if _SKILL_NAME.fullmatch(raw) is None:
        raise SetupError("skill name is invalid")
    return raw


def unpack_skill_zip(
    workspace: Path, data: bytes, *, max_bytes: int | None = None
) -> Path:
    name = inspect_skill_zip(data)
    archive = zipfile.ZipFile(io.BytesIO(data))
    prefix: list[str] = []
    for info in archive.infolist():
        parts = _safe_zip_parts(info.filename)
        if parts and not info.is_dir() and parts[-1].lower() == "skill.md":
            prefix = parts[:-1]
            break
    dest = workspace / ".agents" / "skills" / name
    index = 0
    while dest.exists():
        index += 1
        dest = workspace / ".agents" / "skills" / f"{name}-{index}"
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        parts = _safe_zip_parts(info.filename)
        if parts is None:
            raise SetupError("skill zip path must stay inside the package")
        if prefix:
            if parts[: len(prefix)] != prefix:
                continue
            rel = parts[len(prefix) :]
        else:
            rel = parts
        if not rel:
            continue
        count += 1
        if count > _MAX_SKILL_FILES:
            raise SetupError("skill zip has too many files")
        total += info.file_size
        if max_bytes is not None and total > max_bytes:
            raise SetupError("skill zip exceeds workspace size")
        target = dest.joinpath(*rel)
        if not _is_under(target, dest) and target != dest:
            raise SetupError("skill zip path must stay inside the package")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info.filename))
    if not (dest / "SKILL.md").is_file() and not any(dest.rglob("SKILL.md")):
        raise SetupError("skill zip needs exactly one SKILL.md")
    return dest
