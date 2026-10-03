import io
import uuid
import zipfile
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient
from tests.support.split_worker import api_settings_for, split_client_for
from tests.support.workspace import hosted_dir
from tests.unit.test_blobs import FakeS3

from apipi.common.dirs import pi_session_file
from apipi.common.errors import ObjectStoreError
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.store.blobs import S3Blobs, S3Store
from apipi.store.engine import Store
from apipi.store.repo import get_session
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
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
    )


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}},
        operation,
    )


class _FailPut(FakeS3):
    def put_object(self, **kwargs: object) -> None:
        del kwargs
        raise _client_error("AccessDenied", "PutObject")


class _FailGet(FakeS3):
    def get_object(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        raise _client_error("InternalError", "GetObject")


class _Publish(FakeHarness):
    async def generate(  # type: ignore[override]
        self, text: str, *, cwd: str | None = None, **kwargs: Any
    ):
        if isinstance(cwd, str) and cwd:
            out = Path(cwd) / "outputs"
            out.mkdir(parents=True, exist_ok=True)
            (out / "note.txt").write_text("hello", encoding="utf-8")
        async for item in super().generate(text, cwd=cwd, **kwargs):
            yield item


def _zip_skill() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/SKILL.md", "---\nname: demo\n---\n")
    return buffer.getvalue()


def _error(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "agent.session.error")


async def _events(client: AsyncClient, token: str, session_id: str) -> list[dict]:
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert isinstance(data, list)
    return data


async def test_s3_put_failure_fails_turn(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = _FailPut()

    async def _boom_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        # Split workers upload without store credentials (presigned PUT);
        # the S3 PUT failure surfaces here, not at the API's S3 client.
        raise _client_error("AccessDenied", "PutObject")

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _boom_put)
    async with split_client_for(
        s3_settings,
        store,
        harness=_Publish(),
        blobs=S3Blobs(s3_settings, client=client_s3),
        objects=S3Store(s3_settings, client=client_s3),
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "s3-put"
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "input": "hello"},
        )
        assert created.status_code == 502
        assert created.json()["error"]["code"] == "artifact_store"
        session_id = created.json()["error"]["session_id"]
        got = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
        assert got.status_code == 200
        assert got.json()["status"] == "idle"
        events = await _events(client, token, session_id)
        types = [event["type"] for event in events]
        assert "agent.session.turn.failed" in types
        error = _error(events)
        assert error["data"]["code"] == "artifact_store"
        again = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "again"},
        )
        assert again.status_code == 200


async def test_pi_session_upload_failure_is_artifact_store(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = FakeS3()

    async def _boom_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        raise _client_error("AccessDenied", "PutObject")

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _boom_put)
    async with split_client_for(
        s3_settings,
        store,
        blobs=S3Blobs(s3_settings, client=client_s3),
        objects=S3Store(s3_settings, client=client_s3),
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "s3-cache-write"
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"]},
        )
        session_id = created.json()["id"]
        path = pi_session_file(hosted_dir(s3_settings, token, session_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"ok":true}\n')
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert sent.status_code == 200
        events = await _events(client, token, session_id)
        assert "agent.session.turn.failed" in [event["type"] for event in events]
        assert _error(events)["data"]["code"] == "artifact_store"


async def test_expected_cache_restore_fails_turn(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = _FailGet()

    async def _boom_fetch(ref: object, settings: object) -> bytes:
        # Split workers restore the Pi session over the socket boundary;
        # the blob GET failure surfaces at the context fetch, which the
        # worker maps to artifact_store like a direct store read.
        raise ObjectStoreError(
            "Artifact store unavailable",
            operation="get",
            bucket="bucket",
            key="cache",
            code="AccessDenied",
        )

    monkeypatch.setattr("apipi.worker.turn_context.fetch_pi_session_bytes", _boom_fetch)
    async with split_client_for(
        s3_settings,
        store,
        blobs=S3Blobs(s3_settings, client=client_s3),
        objects=S3Store(s3_settings, client=client_s3),
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "s3-restore"
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
            row = await get_session(db, tenant_id, session_id)
            assert row is not None
            row.pi_session_id = uuid.uuid4()
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert sent.status_code == 200
        got = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
        assert got.json()["status"] == "idle"
        events = await _events(client, token, str(session_id))
        types = [event["type"] for event in events]
        assert "agent.session.turn.failed" in types
        error = _error(events)
        assert error["data"]["code"] == "artifact_store"
        codes = [
            event["data"].get("code")
            for event in events
            if event["type"] == "agent.session.error"
        ]
        assert "internal" not in codes


async def test_workspace_file_s3_get_fails_environment(
    settings: Settings, store: Store
) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = _FailGet()
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "s3-file"
        uploaded = await client.post(
            "/v1/files",
            headers=_auth(token),
            data={"purpose": "user_data"},
            files={"file": ("note.txt", b"hi", "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["id"]
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "input": "hello",
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {
                            "type": "file_id",
                            "file_id": file_id,
                            "path": "/workspace/note.txt",
                        }
                    ],
                },
            },
        )
        assert created.status_code == 503
        assert created.json()["error"]["code"] == "artifact_store"


async def test_skill_s3_get_fails_environment(settings: Settings, store: Store) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = _FailGet()
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    data = _zip_skill()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "s3-skill"
        uploaded = await client.post(
            "/v1/skills",
            headers=_auth(token),
            files={"files": ("demo.zip", data, "application/zip")},
        )
        assert uploaded.status_code == 200
        skill_id = uploaded.json()["id"]
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "input": "hello",
                "environment": {
                    "type": "openai_hosted",
                    "skills": [{"type": "skill_reference", "skill_id": skill_id}],
                },
            },
        )
        assert created.status_code == 503
        assert created.json()["error"]["code"] == "artifact_store"


async def test_file_content_s3_error_is_503(settings: Settings, store: Store) -> None:
    s3_settings = _s3_settings(settings)
    client_s3 = _FailGet()
    app = create_app(
        api_settings_for(s3_settings),
        store=store,
        objects=S3Store(s3_settings, client=client_s3),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "s3-content"
        uploaded = await client.post(
            "/v1/files",
            headers=_auth(token),
            data={"purpose": "user_data"},
            files={"file": ("note.txt", b"hi", "text/plain")},
        )
        assert uploaded.status_code == 200
        content = await client.get(
            f"/v1/files/{uploaded.json()['id']}/content", headers=_auth(token)
        )
        assert content.status_code == 503
        assert content.json()["error"]["code"] == "artifact_store"
