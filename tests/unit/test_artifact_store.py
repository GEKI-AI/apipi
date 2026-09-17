import uuid
from pathlib import Path

import pytest

from apipi.config import DiskLimitError, Settings
from apipi.worker.pi.artifacts import (
    dir_bytes,
    read_workspace_artifacts,
    unpack_artifact_tar,
    unpack_workspace_tar,
    wipe_workspace,
)
from apipi.worker.pi.guest import (
    artifacts_tar_bytes,
    session_file_bytes,
    workspace_tar_bytes,
)


def test_read_workspace_artifacts(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "out.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "outputs").mkdir()
    nested = tmp_path / "outputs" / "dir"
    nested.mkdir()
    (nested / "out.bin").write_bytes(b"xyz")
    (tmp_path / "scratch.txt").write_text("no", encoding="utf-8")
    files = dict(read_workspace_artifacts(tmp_path))
    assert files == {"outputs/dir/out.bin": b"xyz"}


def test_unpack_artifact_tar_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    nested_skip = tmp_path / "artifacts" / "dir"
    nested_skip.mkdir()
    (nested_skip / "a.bin").write_bytes(b"abc")
    (tmp_path / "outputs").mkdir()
    nested = tmp_path / "outputs" / "dir"
    nested.mkdir()
    (nested / "a.bin").write_bytes(b"abc")
    (tmp_path / "outputs" / "out.txt").write_text("z", encoding="utf-8")
    data = artifacts_tar_bytes(tmp_path)
    files = dict(unpack_artifact_tar(data))
    assert files == {"outputs/dir/a.bin": b"abc", "outputs/out.txt": b"z"}


def test_unpack_empty_tar() -> None:
    assert unpack_artifact_tar(b"") == []


def test_dir_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"abcd")
    nested = tmp_path / "dir"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"xy")
    assert dir_bytes(tmp_path) == 6
    assert dir_bytes(tmp_path / "missing") == 0


def test_unpack_workspace_tar_rejects_over_cap(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    dest = tmp_path / "host"
    with pytest.raises(DiskLimitError) as exc:
        unpack_workspace_tar(workspace_tar_bytes(tmp_path), dest, max_bytes=1)
    assert exc.value.code == "workspace_too_large"
    assert not dest.exists() or dir_bytes(dest) == 0


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


def test_session_file_bytes(tmp_path: Path) -> None:
    assert session_file_bytes(tmp_path) == b""
    nested = tmp_path / ".apipi"
    nested.mkdir()
    (nested / "pi-session.jsonl").write_bytes(b'["stay"]')
    assert session_file_bytes(tmp_path) == b'["stay"]'


def test_wipe_workspace(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    wipe_workspace(tmp_path)
    assert not tmp_path.exists()


def test_settings_artifact_blob_path(tmp_path: Path) -> None:
    from apipi.worker.pi.dirs import artifact_blob_path

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        sessions_dir=str(tmp_path),
    )
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    path = artifact_blob_path(settings, tenant, session, artifact, key_id="user-a")
    assert path.parent.is_dir()
    assert path.parent.name == str(session)
    assert path.parent.parent.name == "user-a"
