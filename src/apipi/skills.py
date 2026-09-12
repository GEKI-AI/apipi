import shutil
from pathlib import Path

_WELL_KNOWN = (".agents/skills", ".pi/skills")


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
