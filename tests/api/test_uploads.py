import io
import zipfile
from uuid import NAMESPACE_URL, uuid5

from httpx import ASGITransport, AsyncClient
from tests.unit.test_blobs import FakeS3

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.store.blobs import S3Store, file_object_id, skill_object_id
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _s3_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_prefix="apipi/artifacts",
        s3_endpoint="https://hel1.your-objectstorage.com",
        s3_region="hel1",
    )


def _zip_skill() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/SKILL.md", "---\nname: demo\n---\n")
    return buffer.getvalue()


async def test_presign_requires_s3(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/uploads",
        headers=_auth("t"),
        json={
            "purpose": "file",
            "filename": "a.txt",
            "bytes": 4,
            "content_type": "text/plain",
        },
    )
    assert created.status_code == 400
    assert created.json()["error"]["code"] == "presign_unsupported"


async def test_file_presign_put_then_complete(settings: Settings, store: Store) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        s3_settings,
        store=store,
        harness=FakeHarness(),
        objects=S3Store(s3_settings, client=client_s3),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "up2"
        created = await client.post(
            "/v1/uploads",
            headers=_auth(token),
            json={
                "purpose": "attachment",
                "filename": "a.txt",
                "bytes": 5,
                "content_type": "text/plain",
            },
        )
        assert created.status_code == 200
        file_id = created.json()["object_id"]
        upload_id = created.json()["upload_id"]
        missing = await client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "upload_incomplete"
        tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
        object_id = file_object_id(tenant_id, file_id)
        client_s3.put_object(
            Key=f"apipi/files/{object_id}",
            Body=b"hello",
            ContentType="text/plain",
        )
        done = await client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert done.status_code == 200
        assert done.json()["id"] == file_id
        assert done.json()["bytes"] == 5
        other = await client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers=_auth("other"),
            json={},
        )
        assert other.status_code == 404
        download = await client.post(
            f"/v1/files/{file_id}/download", headers=_auth(token)
        )
        assert download.status_code == 200
        assert download.json()["method"] == "GET"
        assert "presign=1" in download.json()["url"]


async def test_skill_presign_complete(settings: Settings, store: Store) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        s3_settings,
        store=store,
        harness=FakeHarness(),
        objects=S3Store(s3_settings, client=client_s3),
    )
    data = _zip_skill()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "sk"
        created = await client.post(
            "/v1/uploads",
            headers=_auth(token),
            json={
                "purpose": "skill",
                "filename": "demo.zip",
                "bytes": len(data),
                "content_type": "application/zip",
            },
        )
        assert created.status_code == 200
        skill_id = created.json()["object_id"]
        upload_id = created.json()["upload_id"]
        tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
        client_s3.put_object(
            Key=f"apipi/skills/{skill_object_id(tenant_id, skill_id)}",
            Body=data,
            ContentType="application/zip",
        )
        done = await client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert done.status_code == 200
        assert done.json()["id"] == skill_id
        assert done.json()["name"] == "demo"
        download = await client.post(
            f"/v1/skills/{skill_id}/download", headers=_auth(token)
        )
        assert download.status_code == 200
        assert download.json()["method"] == "GET"


async def test_upload_oversize_rejected(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/uploads",
        headers=_auth("t"),
        json={
            "purpose": "file",
            "filename": "big.bin",
            "bytes": 99_000_000_000,
        },
    )
    assert created.status_code == 413
    assert created.json()["error"]["code"] == "payload_too_large"
