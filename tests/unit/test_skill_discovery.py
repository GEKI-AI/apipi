from pathlib import Path

from apipi.skills import copy_capability_directories, discover_skill_dirs


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
