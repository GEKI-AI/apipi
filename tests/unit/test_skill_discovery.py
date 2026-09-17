import io
import zipfile
from pathlib import Path

import pytest

from apipi.env.setup import SetupError
from apipi.skills import (
    copy_capability_directories,
    discover_skill_dirs,
    inspect_skill_zip,
    unpack_skill_zip,
)


def _plant(root: Path, name: str) -> Path:
    tree = root / name
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    return tree


def test_discover_well_known_and_capability_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    agents = _plant(workspace / ".agents" / "skills", "demo")
    pi = _plant(workspace / ".pi" / "skills", "pi-demo")
    caps = tmp_path / "caps"
    cap_skill = _plant(caps, "cap-demo")
    copy_capability_directories(workspace, [str(caps)])
    trees = discover_skill_dirs(workspace, [str(caps)])
    assert str(agents.resolve()) in trees
    assert str(pi.resolve()) in trees
    copied = workspace / "caps" / "cap-demo"
    assert copied.is_dir()
    assert str(copied.resolve()) in trees
    assert str(cap_skill.resolve()) not in trees


def test_copy_skips_missing_and_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    copy_capability_directories(workspace, ["relative", str(tmp_path / "missing")])
    assert list(workspace.iterdir()) == []


def test_discover_empty_without_workspace() -> None:
    assert discover_skill_dirs(None, ["/tmp"]) == []


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return buf.getvalue()


def test_unpack_skill_zip_into_agents_skills(tmp_path: Path) -> None:
    data = _zip_bytes(
        {
            "demo/SKILL.md": "---\nname: demo\n---\nHi\n",
            "demo/scripts/run.sh": "echo ok\n",
        }
    )
    assert inspect_skill_zip(data) == "demo"
    dest = unpack_skill_zip(tmp_path, data)
    assert dest == tmp_path / ".agents" / "skills" / "demo"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "run.sh").is_file()


def test_inspect_rejects_path_traversal() -> None:
    data = _zip_bytes({"../SKILL.md": "---\nname: x\n---\n"})
    with pytest.raises(SetupError, match="inside the package"):
        inspect_skill_zip(data)


def test_inspect_rejects_missing_manifest() -> None:
    data = _zip_bytes({"demo/readme.txt": "nope\n"})
    with pytest.raises(SetupError, match="exactly one"):
        inspect_skill_zip(data)
