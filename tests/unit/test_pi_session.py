import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from apipi.config import Settings
from apipi.store.blobs import LocalBlobs, MemoryBlobs, artifact_blob_uri
from apipi.store.engine import Store
from apipi.store.models import SessionRow
from apipi.store.repo import create_session, create_tenant
from apipi.worker.pi.artifacts import persist_pi_session, restore_pi_session
from apipi.worker.pi.dirs import pi_session_file


def _settings(
    tmp_path: Path,
    *,
    artifact_store: Literal["local", "s3"] = "local",
    s3_bucket: str | None = None,
    s3_prefix: str = "apipi/artifacts",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        artifact_store=artifact_store,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
    )


def _write_cache(dest: Path, data: bytes) -> None:
    path = pi_session_file(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


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


async def test_persist_sets_file_uri(store: Store, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    dest = tmp_path / "ws"
    dest.mkdir()
    _write_cache(dest, b'["stay"]')
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, key_id="user")
        await persist_pi_session(
            db, settings, row, None, dest, blobs=LocalBlobs(settings)
        )
        assert row.pi_session_id is not None
        assert row.pi_session_bytes == 8
        assert row.pi_session_uri is not None
        assert row.pi_session_uri.startswith("file://")
        path = Path(urlparse(row.pi_session_uri).path)
        assert path.is_file()
        assert path.read_bytes() == b'["stay"]'
        assert str(row.pi_session_id) in row.pi_session_uri


async def test_persist_sets_s3_uri(store: Store, tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="cache",
        s3_prefix="apipi/artifacts",
    )
    dest = tmp_path / "ws"
    dest.mkdir()
    _write_cache(dest, b'["stay"]')
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, key_id="user")
        await persist_pi_session(db, settings, row, None, dest, blobs=MemoryBlobs())
        assert row.pi_session_uri is not None
        assert row.pi_session_id is not None
        assert row.pi_session_uri == (
            f"s3://cache/apipi/artifacts/{row.tenant_id}/user/"
            f"{row.id}/{row.pi_session_id}"
        )


def test_artifact_blob_uri_s3_layout(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_prefix="apipi/artifacts",
    )
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    uri = artifact_blob_uri(settings, tenant_id, "user", session_id, blob_id)
    assert uri == (
        f"s3://bucket/apipi/artifacts/{tenant_id}/user/{session_id}/{blob_id}"
    )


async def test_restore_from_file_uri(tmp_path: Path) -> None:
    cache = tmp_path / "cache.bin"
    cache.write_bytes(b'["stay"]')
    dest = tmp_path / "ws"
    dest.mkdir()
    row = SessionRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        key_id="user",
        pi_session_uri=cache.resolve().as_uri(),
    )
    await restore_pi_session(_settings(tmp_path), row, dest)
    assert pi_session_file(dest).read_bytes() == b'["stay"]'
