import io
import uuid
from pathlib import Path
from typing import Literal

import pytest

from apipi.blobs import (
    NS_ARTIFACTS,
    NS_FILES,
    NS_SKILLS,
    LocalBlobs,
    LocalStore,
    MemoryBlobs,
    MemoryStore,
    S3Blobs,
    S3Store,
    blob_key,
    blob_prefix,
    blob_store,
    file_object_id,
    object_store,
    s3_addressing,
    s3_client_kwargs,
    s3_namespace_prefix,
    s3_object_key,
    skill_object_id,
)
from apipi.config import ConfigError, Settings, load_settings


def _settings(
    tmp_path: Path,
    *,
    artifact_store: Literal["local", "s3"] = "local",
    s3_bucket: str | None = None,
    s3_endpoint: str | None = None,
    s3_region: str = "us-east-1",
    s3_prefix: str = "apipi/artifacts",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        artifact_store=artifact_store,
        s3_bucket=s3_bucket,
        s3_endpoint=s3_endpoint,
        s3_region=s3_region,
        s3_prefix=s3_prefix,
    )


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(key, str)
        assert isinstance(body, bytes)
        self.objects[key] = body

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key not in self.objects:
            raise FakeS3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}

    def delete_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        if isinstance(key, str):
            self.objects.pop(key, None)

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = kwargs.get("Prefix", "")
        assert isinstance(prefix, str)
        contents = [
            {"Key": key, "Size": len(data)}
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


async def test_memory_blobs_roundtrip() -> None:
    shared: dict[str, bytes] = {}
    first = MemoryBlobs(shared)
    second = MemoryBlobs(shared)
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    await first.put(tenant, "user-a", session, artifact, b"hello")
    assert await second.get(tenant, "user-a", session, artifact) == b"hello"
    assert await second.used_bytes(tenant, "user-a", session) == 5
    await second.delete(tenant, "user-a", session, artifact)
    assert await first.get(tenant, "user-a", session, artifact) is None


async def test_local_blobs_uses_tenant_and_user(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    blobs = LocalBlobs(settings)
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    await blobs.put(tenant, "abc123", session, artifact, b"xyz")
    path = (
        tmp_path
        / "sessions"
        / ".artifacts"
        / str(tenant)
        / "abc123"
        / str(session)
        / str(artifact)
    )
    assert path.read_bytes() == b"xyz"
    assert await blobs.get(tenant, "abc123", session, artifact) == b"xyz"
    await blobs.delete_session(tenant, "abc123", session)
    assert await blobs.get(tenant, "abc123", session, artifact) is None


async def test_s3_kwargs_hetzner(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="apipi-artifacts",
        s3_endpoint="https://hel1.your-objectstorage.com",
        s3_region="hel1",
    )
    assert s3_addressing(settings) == "path"
    kwargs = s3_client_kwargs(settings)
    assert kwargs["endpoint_url"] == "https://hel1.your-objectstorage.com"
    assert kwargs["region_name"] == "hel1"
    config = kwargs["config_kwargs"]
    assert isinstance(config, dict)
    assert config["s3"] == {"addressing_style": "path"}
    assert config["request_checksum_calculation"] == "when_required"
    assert config["response_checksum_validation"] == "when_required"


async def test_s3_kwargs_aws_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifact_store="s3", s3_bucket="bucket")
    assert s3_addressing(settings) == "virtual"
    kwargs = s3_client_kwargs(settings)
    assert "endpoint_url" not in kwargs


async def test_s3_blobs_with_fake_client(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_prefix="apipi/artifacts",
    )
    client = FakeS3()
    blobs = S3Blobs(settings, client=client)
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    await blobs.put(tenant, "user-a", session, artifact, b"data")
    key = blob_key(tenant, "user-a", session, artifact)
    assert f"apipi/artifacts/{key}" in client.objects
    assert await blobs.get(tenant, "user-a", session, artifact) == b"data"
    assert await blobs.used_bytes(tenant, "user-a", session) == 4
    await blobs.delete_session(tenant, "user-a", session)
    assert await blobs.get(tenant, "user-a", session, artifact) is None


def test_blob_store_local_default(tmp_path: Path) -> None:
    assert isinstance(blob_store(_settings(tmp_path)), LocalBlobs)
    assert isinstance(object_store(_settings(tmp_path)), LocalStore)


async def test_local_store_namespaces(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalStore(settings)
    tenant = uuid.uuid4()
    file_id = "file-abc"
    skill_id = "skill-xyz"
    await store.put(NS_FILES, file_object_id(tenant, file_id), b"file-bytes")
    await store.put(NS_SKILLS, skill_object_id(tenant, skill_id), b"skill-bytes")
    root = tmp_path / "sessions"
    assert (
        root / ".store" / "files" / str(tenant) / file_id
    ).read_bytes() == b"file-bytes"
    assert (
        root / ".store" / "skills" / str(tenant) / skill_id
    ).read_bytes() == b"skill-bytes"
    assert (root / ".artifacts").exists() is False
    assert await store.get(NS_FILES, file_object_id(tenant, file_id)) == b"file-bytes"
    await store.delete_prefix(NS_FILES, str(tenant))
    assert await store.get(NS_FILES, file_object_id(tenant, file_id)) is None
    assert (
        await store.get(NS_SKILLS, skill_object_id(tenant, skill_id)) == b"skill-bytes"
    )


async def test_memory_store_namespaces_isolated() -> None:
    store = MemoryStore()
    tenant = uuid.uuid4()
    object_id = f"{tenant}/same"
    await store.put(NS_FILES, object_id, b"file")
    await store.put(NS_SKILLS, object_id, b"skill")
    assert await store.get(NS_FILES, object_id) == b"file"
    assert await store.get(NS_SKILLS, object_id) == b"skill"
    await store.delete_prefix(NS_FILES, str(tenant))
    assert await store.get(NS_FILES, object_id) is None
    assert await store.get(NS_SKILLS, object_id) == b"skill"


def test_s3_namespace_prefix_siblings(tmp_path: Path) -> None:
    settings = _settings(tmp_path, s3_prefix="apipi/artifacts")
    assert s3_namespace_prefix(settings, NS_ARTIFACTS) == "apipi/artifacts"
    assert s3_namespace_prefix(settings, NS_FILES) == "apipi/files"
    assert s3_namespace_prefix(settings, NS_SKILLS) == "apipi/skills"


def test_s3_namespace_prefix_without_artifacts_suffix(tmp_path: Path) -> None:
    settings = _settings(tmp_path, s3_prefix="bucket-root")
    assert s3_namespace_prefix(settings, NS_ARTIFACTS) == "bucket-root"
    assert s3_namespace_prefix(settings, NS_FILES) == "bucket-root/files"
    assert s3_namespace_prefix(settings, NS_SKILLS) == "bucket-root/skills"


async def test_s3_store_key_layout(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_prefix="apipi/artifacts",
    )
    client = FakeS3()
    store = S3Store(settings, client=client)
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    file_id = "file-abc"
    key = blob_key(tenant, "user-a", session, artifact)
    await store.put(NS_ARTIFACTS, key, b"art")
    await store.put(NS_FILES, file_object_id(tenant, file_id), b"file")
    assert f"apipi/artifacts/{key}" in client.objects
    assert f"apipi/files/{tenant}/{file_id}" in client.objects
    assert s3_object_key(settings, NS_FILES, file_object_id(tenant, file_id)) == (
        f"apipi/files/{tenant}/{file_id}"
    )
    assert await store.get(NS_ARTIFACTS, key) == b"art"
    assert await store.used_bytes(NS_FILES, str(tenant)) == 4
    await store.delete_prefix(NS_ARTIFACTS, blob_prefix(tenant, "user-a", session))
    assert await store.get(NS_ARTIFACTS, key) is None
    assert await store.get(NS_FILES, file_object_id(tenant, file_id)) == b"file"


def test_s3_bucket_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_RUN_MODE", "none")
    monkeypatch.setenv("APIPI_ARTIFACT_STORE", "s3")
    with pytest.raises(ConfigError, match="APIPI_S3_BUCKET is required"):
        load_settings()
