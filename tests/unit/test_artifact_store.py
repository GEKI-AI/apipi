import uuid
from pathlib import Path

from apipi.config import Settings
from apipi.pi.artifacts import (
    read_workspace_artifacts,
    unpack_artifact_tar,
    unpack_workspace_tar,
    wipe_workspace,
)
from apipi.pi.guest import artifacts_tar_bytes, workspace_tar_bytes


def test_read_workspace_artifacts(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "out.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "out.bin").write_bytes(b"xyz")
    (tmp_path / "scratch.txt").write_text("no", encoding="utf-8")
    files = dict(read_workspace_artifacts(tmp_path))
    assert files == {"artifacts/out.txt": b"hi", "outputs/out.bin": b"xyz"}


def test_unpack_artifact_tar_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    nested = tmp_path / "artifacts" / "dir"
    nested.mkdir()
    (nested / "a.bin").write_bytes(b"abc")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "out.txt").write_text("z", encoding="utf-8")
    data = artifacts_tar_bytes(tmp_path)
    files = dict(unpack_artifact_tar(data))
    assert files["artifacts/dir/a.bin"] == b"abc"
    assert files["outputs/out.txt"] == b"z"


def test_unpack_empty_tar() -> None:
    assert unpack_artifact_tar(b"") == []


def test_unpack_workspace_tar_skips_apipi(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    nested = tmp_path / "dir"
    nested.mkdir()
    (nested / "a.bin").write_bytes(b"abc")
    (tmp_path / ".apipi").mkdir()
    (tmp_path / ".apipi" / "env").write_text("secret", encoding="utf-8")
    dest = tmp_path / "host"
    unpack_workspace_tar(workspace_tar_bytes(tmp_path), dest)
    assert (dest / "note.txt").read_text(encoding="utf-8") == "hello"
    assert (dest / "dir" / "a.bin").read_bytes() == b"abc"
    assert not (dest / ".apipi").exists()


def test_wipe_workspace(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    wipe_workspace(tmp_path)
    assert not tmp_path.exists()


def test_settings_artifact_blob_path(tmp_path: Path) -> None:
    from apipi.pi.dirs import artifact_blob_path

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        sessions_dir=str(tmp_path),
    )
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    path = artifact_blob_path(settings, tenant, session, artifact)
    assert path.parent.is_dir()
    assert path.parent.name == str(session)
