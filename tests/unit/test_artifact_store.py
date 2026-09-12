import uuid
from pathlib import Path

from apipi.config import Settings
from apipi.pi.artifacts import (
    read_workspace_artifacts,
    unpack_artifact_tar,
    wipe_workspace,
)
from apipi.pi.guest import artifacts_tar_bytes


def test_read_workspace_artifacts(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "out.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("no", encoding="utf-8")
    files = dict(read_workspace_artifacts(tmp_path))
    assert files == {"artifacts/out.txt": b"hi"}


def test_unpack_artifact_tar_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    nested = tmp_path / "artifacts" / "dir"
    nested.mkdir()
    (nested / "a.bin").write_bytes(b"abc")
    data = artifacts_tar_bytes(tmp_path)
    files = dict(unpack_artifact_tar(data))
    assert files["artifacts/dir/a.bin"] == b"abc"


def test_unpack_empty_tar() -> None:
    assert unpack_artifact_tar(b"") == []


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
