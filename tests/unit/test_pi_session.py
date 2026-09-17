import uuid
from pathlib import Path

from apipi.config import Settings
from apipi.store.blobs import MemoryBlobs
from apipi.store.models import SessionRow
from apipi.worker.pi.artifacts import restore_pi_session
from apipi.worker.pi.dirs import pi_session_file


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )


async def test_restore_pi_session_writes_file(tmp_path: Path) -> None:
    blobs = MemoryBlobs()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    await blobs.put(tenant_id, "user", session_id, blob_id, b'["stay"]')
    row = SessionRow(
        id=session_id,
        tenant_id=tenant_id,
        key_id="user",
        pi_session_id=blob_id,
        pi_session_bytes=8,
    )
    dest = tmp_path / "ws"
    dest.mkdir()
    await restore_pi_session(_settings(tmp_path), row, dest, blobs=blobs)
    assert pi_session_file(dest).read_bytes() == b'["stay"]'


async def test_restore_pi_session_skips_without_pointer(tmp_path: Path) -> None:
    row = SessionRow(id=uuid.uuid4(), tenant_id=uuid.uuid4(), key_id="")
    dest = tmp_path / "ws"
    dest.mkdir()
    await restore_pi_session(_settings(tmp_path), row, dest, blobs=MemoryBlobs())
    assert not pi_session_file(dest).exists()
