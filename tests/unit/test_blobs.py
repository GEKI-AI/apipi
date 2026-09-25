import io
import logging
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from apipi.config import ConfigError, Settings, load_settings
from apipi.store.blobs import (
    NS_ARTIFACTS,
    NS_FILES,
    NS_SKILLS,
    ArtifactAdapter,
    LocalBlobs,
    LocalStore,
    MemoryBlobs,
    MemoryStore,
    ObjectStoreError,
    S3Blobs,
    S3Store,
    _give_to_operator,
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
from apipi.store.disposition import content_disposition


def _settings(
    tmp_path: Path,
    *,
    artifact_store: Literal["local", "s3"] = "local",
    s3_bucket: str | None = None,
    s3_endpoint: str | None = None,
    s3_region: str = "us-east-1",
    s3_prefix: str = "apipi/artifacts",
    s3_addressing: Literal["auto", "path", "virtual"] = "auto",
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
        s3_addressing=s3_addressing,
    )


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.presigns: list[dict[str, str]] = []

    def put_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(key, str)
        assert isinstance(body, bytes)
        self.objects[key] = body
        ctype = kwargs.get("ContentType")
        if isinstance(ctype, str):
            self.types[key] = ctype

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key not in self.objects:
            raise FakeS3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key not in self.objects:
            raise FakeS3Error("404")
        return {
            "ContentLength": len(self.objects[key]),
            "ContentType": self.types.get(key, "application/octet-stream"),
        }

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, str],
        ExpiresIn: int = 900,
        HttpMethod: str | None = None,
    ) -> str:
        del ClientMethod, ExpiresIn
        self.presigns.append(dict(Params))
        key = Params["Key"]
        method = HttpMethod or "GET"
        query = {"presign": "1", "method": method}
        disposition = Params.get("ResponseContentDisposition")
        if isinstance(disposition, str):
            query["response-content-disposition"] = disposition
        response_type = Params.get("ResponseContentType")
        if isinstance(response_type, str):
            query["response-content-type"] = response_type
        return f"https://bucket.example/{key}?{urlencode(query)}"

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
    assert s3_addressing(settings) == "virtual"
    kwargs = s3_client_kwargs(settings)
    assert kwargs["endpoint_url"] == "https://hel1.your-objectstorage.com"
    assert kwargs["region_name"] == "hel1"
    config = kwargs["config_kwargs"]
    assert isinstance(config, dict)
    assert config["signature_version"] == "s3v4"
    assert config["s3"] == {
        "addressing_style": "virtual",
        "payload_signing_enabled": False,
    }
    assert config["request_checksum_calculation"] == "when_required"
    assert config["response_checksum_validation"] == "when_required"


async def test_s3_kwargs_aws_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifact_store="s3", s3_bucket="bucket")
    assert s3_addressing(settings) == "virtual"
    kwargs = s3_client_kwargs(settings)
    assert "endpoint_url" not in kwargs


def test_s3_addressing_path_opt_in(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_endpoint="https://minio.example:9000",
        s3_addressing="path",
    )
    assert s3_addressing(settings) == "path"


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


def test_root_writer_gives_files_to_sudo_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    nested = root / ".artifacts" / "tenant" / "file.bin"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "operator")

    class _Pw:
        pw_uid = 1000
        pw_gid = 1000

    monkeypatch.setattr("apipi.store.blobs.pwd.getpwnam", lambda _name: _Pw())
    chowned: list[Path] = []

    def fake_chown(path: Path, uid: int, gid: int) -> None:
        del uid, gid
        chowned.append(Path(path))

    class _Stat:
        st_uid = 0

    monkeypatch.setattr(os, "chown", fake_chown)
    monkeypatch.setattr(os, "stat", lambda _path: _Stat())
    _give_to_operator(root, nested)
    assert nested in chowned
    assert nested.parent in chowned


def test_content_disposition_ascii_and_unicode() -> None:
    plain = content_disposition("report.html")
    assert plain == 'attachment; filename="report.html"'
    name = "Bericht_Größe_✓.pdf"
    header = content_disposition(name)
    assert header.startswith("attachment;")
    assert 'filename="Bericht_Gr__e__.pdf"' in header
    assert f"filename*=UTF-8''{quote(name, safe='')}" in header
    assert "inline" not in header


def test_content_disposition_sanitizes() -> None:
    header = content_disposition('../secret"\r\n.html')
    assert header == 'attachment; filename="secret.html"'
    assert "\r" not in header
    assert "\n" not in header
    assert "../" not in header
    assert content_disposition("") == 'attachment; filename="download"'
    assert content_disposition("..") == 'attachment; filename="download"'


def test_presign_get_forces_download(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, artifact_store="s3", s3_bucket="bucket", s3_prefix="apipi/artifacts"
    )
    client = FakeS3()
    store = S3Store(settings, client=client)
    url, _headers = store.presign(
        "GET",
        NS_FILES,
        "tenant/file-1",
        expires=timedelta(minutes=5),
        filename="report.html",
        content_type="text/html",
    )
    params = client.presigns[-1]
    assert params["ResponseContentDisposition"] == 'attachment; filename="report.html"'
    assert params["ResponseContentType"] == "application/octet-stream"
    assert "response-content-disposition" in url
    assert "inline" not in params["ResponseContentDisposition"]
    svg_url, _headers = store.presign(
        "GET",
        NS_FILES,
        "tenant/file-2",
        expires=timedelta(minutes=5),
        filename="icon.svg",
        content_type="image/svg+xml",
    )
    del svg_url
    assert client.presigns[-1]["ResponseContentDisposition"].startswith("attachment;")
    assert client.presigns[-1]["ResponseContentType"] == "application/octet-stream"
    store.presign(
        "GET",
        NS_FILES,
        "tenant/file-3",
        expires=timedelta(minutes=5),
        filename="notes.txt",
        content_type="text/plain",
    )
    assert client.presigns[-1]["ResponseContentType"] == "text/plain"
    store.presign(
        "GET",
        NS_FILES,
        "tenant/file-4",
        expires=timedelta(minutes=5),
        filename="notes.txt",
    )
    assert "ResponseContentType" not in client.presigns[-1]


async def test_artifact_adapter_put_content_type(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, artifact_store="s3", s3_bucket="bucket", s3_prefix="apipi/artifacts"
    )
    client = FakeS3()
    blobs = ArtifactAdapter(S3Store(settings, client=client))
    tenant = uuid.uuid4()
    session = uuid.uuid4()
    artifact = uuid.uuid4()
    await blobs.put(
        tenant, "user-a", session, artifact, b"<p>hi</p>", content_type="text/html"
    )
    key = f"apipi/artifacts/{blob_key(tenant, 'user-a', session, artifact)}"
    assert client.types[key] == "text/html"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": "AWS_SECRET_ACCESS_KEY=supersecret",
            }
        },
        operation,
    )


class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def put_object(self, **kwargs: object) -> None:
        del kwargs
        raise self.exc

    def get_object(self, **kwargs: object) -> None:
        del kwargs
        raise self.exc

    def list_objects_v2(self, **kwargs: object) -> None:
        del kwargs
        raise self.exc


async def test_s3_errors_become_store_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, artifact_store="s3", s3_bucket="bucket")
    denied = S3Store(settings, client=_Boom(_client_error("AccessDenied", "PutObject")))
    caplog.set_level(logging.ERROR, logger="apipi")
    with pytest.raises(ObjectStoreError) as put_exc:
        await denied.put(NS_FILES, "tenant/file-1", b"x")
    assert put_exc.value.code == "AccessDenied"
    assert put_exc.value.operation == "put"
    assert put_exc.value.bucket == "bucket"
    assert put_exc.value.key.endswith("tenant/file-1")
    record = next(
        item
        for item in caplog.records
        if item.__dict__.get("event") == "store.s3.error"
    )
    assert record.__dict__["operation"] == "put"
    assert record.__dict__["bucket"] == "bucket"
    assert record.__dict__["s3_code"] == "AccessDenied"
    assert "supersecret" not in caplog.text
    internal = S3Store(
        settings, client=_Boom(_client_error("InternalError", "GetObject"))
    )
    with pytest.raises(ObjectStoreError) as get_exc:
        await internal.get(NS_FILES, "tenant/file-1")
    assert get_exc.value.code == "InternalError"
    missing = S3Store(settings, client=_Boom(_client_error("NoSuchKey", "GetObject")))
    assert await missing.get(NS_FILES, "tenant/missing") is None
    offline = S3Store(
        settings,
        client=_Boom(EndpointConnectionError(endpoint_url="https://s3.example")),
    )
    with pytest.raises(ObjectStoreError) as list_exc:
        await offline.used_bytes(NS_FILES, "tenant")
    assert list_exc.value.operation == "list"
    assert list_exc.value.code == "EndpointConnectionError"
