import base64
import io
import uuid
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

import pytest
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient
from tests.support.split_worker import api_settings_for, split_client_for
from tests.unit.test_blobs import FakeS3

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthRequest, tenant_from_key
from apipi.gateway.tokens import hash_token
from apipi.store.blobs import S3Store, file_object_id, skill_object_id
from apipi.store.engine import Store
from apipi.store.repo import create_artifact
from apipi.worker.fake_harness import FakeHarness


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


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _key(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _zip_skill() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/SKILL.md", "---\nname: demo\n---\n")
    return buffer.getvalue()


async def test_presign_requires_s3(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/apipi/uploads",
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
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "up2"
        created = await client.post(
            "/v1/apipi/uploads",
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
            f"/v1/apipi/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "upload_incomplete"
        tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
        upload_key = f"apipi/uploads/{tenant_id}/{upload_id}"
        assert _key(created.json()["url"]) == upload_key
        put = client_s3.put_url(created.json()["url"], b"hello", "text/plain")
        assert put == 200
        done = await client.post(
            f"/v1/apipi/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert done.status_code == 200
        assert done.json()["id"] == file_id
        assert done.json()["bytes"] == 5
        object_id = file_object_id(tenant_id, file_id)
        assert client_s3.objects[f"apipi/files/{object_id}"] == b"hello"
        assert upload_key not in client_s3.objects
        other = await client.post(
            f"/v1/apipi/uploads/{upload_id}/complete",
            headers=_auth("other"),
            json={},
        )
        assert other.status_code == 404
        download = await client.post(
            f"/v1/apipi/files/{file_id}/download", headers=_auth(token)
        )
        assert download.status_code == 200
        assert download.json()["method"] == "GET"
        query = _query(download.json()["url"])
        assert "presign=1" in download.json()["url"]
        disposition = query["response-content-disposition"][0]
        assert disposition == 'attachment; filename="a.txt"'
        assert query["response-content-type"] == ["text/plain"]


async def test_skill_presign_complete(settings: Settings, store: Store) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    data = _zip_skill()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "sk"
        created = await client.post(
            "/v1/apipi/uploads",
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
        put = client_s3.put_url(created.json()["url"], data, "application/zip")
        assert put == 200
        done = await client.post(
            f"/v1/apipi/uploads/{upload_id}/complete",
            headers=_auth(token),
            json={},
        )
        assert done.status_code == 200
        assert done.json()["id"] == skill_id
        assert done.json()["name"] == "demo"
        key = f"apipi/skills/{skill_object_id(tenant_id, skill_id)}"
        assert client_s3.objects[key] == data
        download = await client.post(
            f"/v1/apipi/skills/{skill_id}/download", headers=_auth(token)
        )
        assert download.status_code == 200
        assert download.json()["method"] == "GET"
        query = _query(download.json()["url"])
        assert query["response-content-disposition"] == [
            'attachment; filename="demo.zip"'
        ]
        assert query["response-content-type"] == ["application/zip"]


async def test_upload_oversize_rejected(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/apipi/uploads",
        headers=_auth("t"),
        json={
            "purpose": "file",
            "filename": "big.bin",
            "bytes": 99_000_000_000,
        },
    )
    assert created.status_code == 413
    assert created.json()["error"]["code"] == "payload_too_large"


async def test_artifact_download_forces_attachment(
    settings: Settings, store: Store
) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "art-dl"
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"]},
        )
        assert created.status_code == 200
        session_id = uuid.UUID(created.json()["id"])
        tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
        async with store.session() as db:
            artifact = await create_artifact(
                db,
                tenant_id,
                session_id,
                path="outputs/report.html",
                content_type="text/html",
            )
            artifact_id = artifact.id
        download = await client.post(
            f"/v1/apipi/sessions/{session_id}/artifacts/{artifact_id}/download",
            headers=_auth(token),
        )
        assert download.status_code == 200
        query = _query(download.json()["url"])
        disposition = query["response-content-disposition"][0]
        assert disposition == 'attachment; filename="report.html"'
        assert "inline" not in disposition
        assert query["response-content-type"] == ["application/octet-stream"]


class _Users:
    def __call__(self, token: str, request: AuthRequest) -> dict[str, object]:
        user = request.headers.get("x-end-user")
        return {
            "key_id": hash_token(token),
            "tenant_id": tenant_from_key(token),
            "user_id": user,
            "cache_key": f"{token}:{user}",
        }

    def cache_key(self, token: str, request: AuthRequest) -> str:
        return f"{token}:{request.headers.get('x-end-user')}"


async def test_upload_purpose_sets_the_file_kind(
    settings: Settings, store: Store
) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
        authenticate=_Users(),
    )
    token = "kinds"
    headers = {**_auth(token), "X-End-User": "ada"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        ids: dict[str, str] = {}
        for purpose in ("file", "attachment"):
            created = await client.post(
                "/v1/apipi/uploads",
                headers=headers,
                json={
                    "purpose": purpose,
                    "filename": f"{purpose}.txt",
                    "bytes": 5,
                    "content_type": "text/plain",
                },
            )
            assert created.status_code == 200
            assert created.json()["file_purpose"] is None
            file_id = created.json()["object_id"]
            client_s3.put_url(created.json()["url"], b"hello", "text/plain")
            done = await client.post(
                f"/v1/apipi/uploads/{created.json()['upload_id']}/complete",
                headers=headers,
                json={},
            )
            assert done.status_code == 200
            assert done.json()["id"] == file_id
            again = await client.post(
                f"/v1/apipi/uploads/{created.json()['upload_id']}/complete",
                headers=headers,
                json={},
            )
            assert again.json()["id"] == file_id
            ids[purpose] = file_id
        default = await client.get("/v1/files", headers=headers)
        listed = await client.get("/v1/apipi/files", headers=headers)
        attachments = await client.get(
            "/v1/apipi/files", headers=headers, params={"kind": "attachment"}
        )
        content = await client.get(
            f"/v1/files/{ids['attachment']}/content", headers=headers
        )
    assert [row["id"] for row in default.json()["data"]] == [ids["file"]]
    kinds = {row["id"]: row["kind"] for row in listed.json()["data"]}
    assert kinds == {ids["file"]: "file", ids["attachment"]: "attachment"}
    assert [row["id"] for row in attachments.json()["data"]] == [ids["attachment"]]
    assert attachments.json()["data"][0]["user_id"] == "ada"
    assert attachments.json()["data"][0]["purpose"] == "user_data"
    assert content.content == b"hello"


async def test_image_upload_is_an_image_within_the_image_limits(
    settings: Settings, store: Store
) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings).model_copy(update={"max_image_bytes": 16})
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    token = "images"
    tenant_id = uuid5(NAMESPACE_URL, hash_token(token))

    async def _create(
        client: AsyncClient, size: int, content_type: str, **extra: str
    ) -> Any:
        return await client.post(
            "/v1/apipi/uploads",
            headers=_auth(token),
            json={
                "purpose": "image",
                "filename": "photo.png",
                "bytes": size,
                "content_type": content_type,
                **extra,
            },
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await _create(client, 8, "image/png")
        assert created.status_code == 200
        assert created.json()["file_purpose"] == "vision"
        file_id = created.json()["object_id"]
        client_s3.put_url(created.json()["url"], b"\x89PNG1234", "image/png")
        mismatched = await client.post(
            f"/v1/apipi/uploads/{created.json()['upload_id']}/complete",
            headers=_auth(token),
            json={"file_purpose": "user_data"},
        )
        done = await client.post(
            f"/v1/apipi/uploads/{created.json()['upload_id']}/complete",
            headers=_auth(token),
            json={"file_purpose": "vision"},
        )
        plain = await client.post(
            "/v1/apipi/uploads",
            headers=_auth(token),
            json={"purpose": "file", "filename": "a.txt", "bytes": 5},
        )
        plain_id = plain.json()["object_id"]
        client_s3.put_url(plain.json()["url"], b"hello", "application/octet-stream")
        plain_vision = await client.post(
            f"/v1/apipi/uploads/{plain.json()['upload_id']}/complete",
            headers=_auth(token),
            json={"file_purpose": "vision"},
        )
        plain_done = await client.post(
            f"/v1/apipi/uploads/{plain.json()['upload_id']}/complete",
            headers=_auth(token),
            json={"file_purpose": "assistants"},
        )
        not_image = await _create(client, 8, "text/plain")
        too_big = await _create(client, 17, "image/png")
        wrong_purpose = await _create(client, 8, "image/png", file_purpose="user_data")
        vision_file = await client.post(
            "/v1/apipi/uploads",
            headers=_auth(token),
            json={
                "purpose": "file",
                "filename": "photo.png",
                "bytes": 8,
                "content_type": "image/png",
                "file_purpose": "vision",
            },
        )
        lying = await _create(client, 8, "image/png")
        lying_id = lying.json()["object_id"]
        lying_key = _key(lying.json()["url"])
        client_s3.put_object(Key=lying_key, Body=b"x" * 32, ContentType="image/png")
        oversized = await client.post(
            f"/v1/apipi/uploads/{lying.json()['upload_id']}/complete",
            headers=_auth(token),
            json={},
        )
        lying_kept = lying_key in client_s3.objects
        lying_file = f"apipi/files/{file_object_id(tenant_id, lying_id)}"
        lying_copied = lying_file in client_s3.objects
        listed = await client.get(
            "/v1/apipi/files", headers=_auth(token), params={"kind": "image"}
        )
        default = await client.get("/v1/files", headers=_auth(token))
    assert mismatched.status_code == 400
    assert done.status_code == 200
    assert done.json()["purpose"] == "vision"
    assert plain_vision.status_code == 400
    assert plain_done.json()["purpose"] == "assistants"
    assert not_image.status_code == 400
    assert too_big.status_code == 413
    assert wrong_purpose.status_code == 400
    assert vision_file.status_code == 400
    assert oversized.status_code == 413
    assert lying_kept is False
    assert lying_copied is False
    assert [row["id"] for row in listed.json()["data"]] == [file_id]
    assert [row["id"] for row in default.json()["data"]] == [plain_id]


async def test_uploads_and_downloads_of_user_files_match_the_user(
    settings: Settings, store: Store
) -> None:
    client_s3 = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
        authenticate=_Users(),
    )
    token = "upload-users"
    u1 = {**_auth(token), "X-End-User": "u1"}
    u2 = {**_auth(token), "X-End-User": "u2"}

    async def _put(client: AsyncClient, headers: dict[str, str], purpose: str) -> Any:
        created = await client.post(
            "/v1/apipi/uploads",
            headers=headers,
            json={
                "purpose": purpose,
                "filename": f"{purpose}.txt",
                "bytes": 5,
                "content_type": "text/plain",
            },
        )
        assert created.status_code == 200, created.json()
        client_s3.put_url(created.json()["url"], b"hello", "text/plain")
        return created.json()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        attachment = await _put(client, u1, "attachment")
        complete = f"/v1/apipi/uploads/{attachment['upload_id']}/complete"
        u2_complete = await client.post(complete, headers=u2, json={})
        u1_complete = await client.post(complete, headers=u1, json={})
        u2_again = await client.post(complete, headers=u2, json={})
        download = f"/v1/apipi/files/{attachment['object_id']}/download"
        u2_download = await client.post(download, headers=u2)
        u1_download = await client.post(download, headers=u1)
        none_download = await client.post(download, headers=_auth(token))
        agent_file = await _put(client, u1, "file")
        await client.post(
            f"/v1/apipi/uploads/{agent_file['upload_id']}/complete",
            headers=u1,
            json={},
        )
        u2_agent_file = await client.post(
            f"/v1/apipi/files/{agent_file['object_id']}/download", headers=u2
        )
        shared = await _put(client, _auth(token), "attachment")
        u2_shared = await client.post(
            f"/v1/apipi/uploads/{shared['upload_id']}/complete", headers=u2, json={}
        )
    assert u2_complete.status_code == 404
    assert u1_complete.status_code == 200
    assert u2_again.status_code == 404
    assert u2_download.status_code == 404
    assert u1_download.status_code == 200
    assert none_download.status_code == 200
    assert u2_agent_file.status_code == 200
    assert u2_shared.status_code == 200


async def _presigned(
    client: AsyncClient,
    fake: FakeS3,
    token: str,
    purpose: str,
    filename: str,
    data: bytes,
    content_type: str,
) -> dict[str, Any]:
    created = await client.post(
        "/v1/apipi/uploads",
        headers=_auth(token),
        json={
            "purpose": purpose,
            "filename": filename,
            "bytes": len(data),
            "content_type": content_type,
        },
    )
    assert created.status_code == 200, created.json()
    assert fake.put_url(created.json()["url"], data, content_type) == 200
    done = await client.post(
        f"/v1/apipi/uploads/{created.json()['upload_id']}/complete",
        headers=_auth(token),
        json={},
    )
    assert done.status_code == 200, done.json()
    return {**created.json(), "content_type": content_type}


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": list(parts)}],
            }
        ]
    }


async def test_a_put_after_complete_does_not_change_what_the_id_delivers(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3_settings = _s3_settings(settings).model_copy(
        update={"model_registry": {"test": {"input": ["text", "image"]}}}
    )
    fake = FakeS3()

    async def _fetch(
        ref: Any, settings: Settings, *, limit: int | None = None
    ) -> bytes:
        del settings, limit
        data = fake.objects.get(_key(ref["url"]))
        assert data is not None
        return data

    monkeypatch.setattr("apipi.worker.turn_context.fetch_ref_bytes", _fetch)
    harness = FakeHarness()
    token = "re-put"
    tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
    png = b"\x89PNG\r\n\x1a\nimage"
    skill = _zip_skill()
    async with split_client_for(
        s3_settings,
        store,
        harness=harness,
        token=worker_secret,
        objects=S3Store(s3_settings, client=fake),
    ) as (_app, client, _worker):
        text = await _presigned(
            client, fake, token, "attachment", "notes.txt", b"hello", "text/plain"
        )
        image = await _presigned(
            client, fake, token, "image", "a.png", png, "image/png"
        )
        bundle = await _presigned(
            client, fake, token, "skill", "demo.zip", skill, "application/zip"
        )
        again = [
            fake.put_url(upload["url"], b"\xff" * size, upload["content_type"])
            for upload, size in ((text, 5), (image, len(png)), (bundle, len(skill)))
        ]
        content = await client.get(
            f"/v1/files/{text['object_id']}/content", headers=_auth(token)
        )
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        session = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        sent = await client.post(
            f"/v1/agents/sessions/{session.json()['id']}/events",
            headers=_auth(token),
            json=_message(
                {"type": "input_file", "file_id": text["object_id"]},
                {"type": "input_image", "file_id": image["object_id"]},
            ),
        )
    skill_key = f"apipi/skills/{skill_object_id(tenant_id, bundle['object_id'])}"
    image_key = f"apipi/files/{file_object_id(tenant_id, image['object_id'])}"
    assert again == [200, 200, 200]
    assert content.content == b"hello"
    assert sent.status_code == 200, sent.json()
    assert harness.prompts == ['<file name="notes.txt">\nhello\n</file>']
    assert base64.b64decode(harness.images[0][0]["data"]) == png
    assert fake.objects[image_key] == png
    assert fake.objects[skill_key] == skill
    assert fake.objects[_key(text["url"])] == b"\xff" * 5


async def test_a_put_of_another_size_than_declared_is_rejected(
    settings: Settings, store: Store
) -> None:
    fake = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=fake),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/apipi/uploads",
            headers=_auth("size"),
            json={
                "purpose": "file",
                "filename": "a.txt",
                "bytes": 5,
                "content_type": "text/plain",
            },
        )
        url = created.json()["url"]
        larger = fake.put_url(url, b"hello!", "text/plain")
        path = f"/v1/apipi/uploads/{created.json()['upload_id']}/complete"
        complete = await client.post(path, headers=_auth("size"), json={})
        fake.put_object(Key=_key(url), Body=b"hello!")
        unsigned = await client.post(path, headers=_auth("size"), json={})
    assert fake.presigns[-1]["ContentLength"] == 5
    assert larger == 403
    assert complete.status_code == 400
    assert complete.json()["error"]["code"] == "upload_incomplete"
    assert unsigned.status_code == 413
    assert unsigned.json()["error"]["code"] == "payload_too_large"
    assert _key(url) not in fake.objects


class _FailCopyOnce(FakeS3):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def copy_object(self, **kwargs: object) -> None:
        if not self.failed:
            self.failed = True
            raise ClientError({"Error": {"Code": "InternalError"}}, "CopyObject")
        super().copy_object(**kwargs)


async def test_complete_after_a_failed_copy_can_be_retried(
    settings: Settings, store: Store
) -> None:
    fake = _FailCopyOnce()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=fake),
    )
    data = _zip_skill()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/apipi/uploads",
            headers=_auth("retry"),
            json={
                "purpose": "skill",
                "filename": "demo.zip",
                "bytes": len(data),
                "content_type": "application/zip",
            },
        )
        fake.put_url(created.json()["url"], data, "application/zip")
        path = f"/v1/apipi/uploads/{created.json()['upload_id']}/complete"
        failed = await client.post(path, headers=_auth("retry"), json={})
        missing = await client.get(
            f"/v1/skills/{created.json()['object_id']}", headers=_auth("retry")
        )
        done = await client.post(path, headers=_auth("retry"), json={})
        again = await client.post(path, headers=_auth("retry"), json={})
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "artifact_store"
    assert missing.status_code == 404
    assert done.status_code == 200
    assert again.json()["id"] == done.json()["id"]
    assert _key(created.json()["url"]) not in fake.objects


async def test_reads_of_a_stored_object_stop_at_its_size(
    settings: Settings, store: Store
) -> None:
    fake = FakeS3()
    s3_settings = _s3_settings(settings)
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=fake),
    )
    token = "bounded"
    tenant_id = uuid5(NAMESPACE_URL, hash_token(token))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        upload = await _presigned(
            client, fake, token, "file", "notes.txt", b"hello", "text/plain"
        )
        file_id = upload["object_id"]
        key = f"apipi/files/{file_object_id(tenant_id, file_id)}"
        fake.objects[key] = b"x" * 1_000_000
        content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        session = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        text = await client.post(
            f"/v1/agents/sessions/{session.json()['id']}/events",
            headers=_auth(token),
            json=_message({"type": "input_file", "file_id": file_id}),
        )
        hosted = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {"type": "file_id", "file_id": file_id, "path": "notes.txt"}
                    ],
                },
            },
        )
        fake.objects[key] = b"hi"
        smaller = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    for response in (content, text, hosted, smaller):
        assert response.status_code == 503, response.json()
        assert response.json()["error"]["code"] == "artifact_store"
    assert set(fake.ranges) == {"bytes=0-5"}
