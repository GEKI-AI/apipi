import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.support.split_worker import split_client_for
from tests.unit.test_blobs import FakeS3

from apipi.config import Settings
from apipi.store.blobs import S3Blobs, S3Store
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import create_artifact
from apipi.worker.pi.artifacts import reap_workspaces
from apipi.worker.pi.dirs import store_root


def _token(name: str = "t") -> str:
    return name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _hosted_session(client: AsyncClient, token: str) -> tuple[str, Path]:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"]},
    )
    assert created.status_code == 200
    from typing import Any, cast

    from httpx import ASGITransport
    from tests.support.workspace import hosted_dir

    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = cast(Any, transport.app)
    settings = app.state.gateway.settings
    directory = hosted_dir(settings, token, created.json()["id"])
    return str(created.json()["id"]), directory


async def _publish(client: AsyncClient, token: str, session_id: str) -> None:
    turned = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "publish"},
    )
    assert turned.status_code == 200


async def _events(client: AsyncClient, token: str, session_id: str) -> list[dict]:
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert listed.status_code == 200
    return listed.json()["data"]


async def test_write_host_file_and_fetch_content(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token, session_id)
    assert directory.exists()

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["session_id"] == session_id
    assert data[0]["path"] == "outputs/note.txt"
    artifact_id = data[0]["id"]

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    assert content.content == b"hello"
    assert content.headers["content-disposition"] == 'attachment; filename="note.txt"'
    assert content.headers["x-content-type-options"] == "nosniff"


async def test_published_artifact_content_type_is_guessed(client: AsyncClient) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    data = listed.json()["data"]
    assert data[0]["content_type"] == "text/plain"
    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{data[0]['id']}/content",
        headers=_auth(token),
    )
    assert content.headers["content-type"].startswith("text/plain")


async def test_artifact_content_disposition_non_ascii(
    client: AsyncClient,
) -> None:
    token = "art-unicode"
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "Bericht_Größe_✓.txt").write_text("hi", encoding="utf-8")
    await _publish(client, token, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    artifact_id = listed.json()["data"][0]["id"]
    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    disposition = content.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert content.headers["x-content-type-options"] == "nosniff"


async def test_publish_puts_guessed_content_type(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_s3 = FakeS3()
    s3_settings = settings.model_copy(
        update={
            "artifact_store": "s3",
            "s3_bucket": "bucket",
            "s3_endpoint": "https://s3.example",
            "s3_region": "us-east-1",
        }
    )

    async def _fake_put(
        url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> None:
        last = client_s3.presigns[-1]
        client_s3.objects[last["Key"]] = data

    monkeypatch.setattr("apipi.worker.artifact_upload.put_via_url", _fake_put)
    async with split_client_for(
        s3_settings,
        store,
        blobs=S3Blobs(s3_settings, client=client_s3),
        objects=S3Store(s3_settings, client=client_s3),
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "art-ctype"
        session_id, directory = await _hosted_session(client, token)
        (directory / "outputs").mkdir()
        (directory / "outputs" / "page.html").write_text("<p>hi</p>", encoding="utf-8")
        await _publish(client, token, session_id)
        assert "text/html" in {
            presign.get("ContentType") for presign in client_s3.presigns
        }
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        assert listed.json()["data"][0]["content_type"] == "text/html"


async def test_harvest_skips_workspace_artifacts_folder(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("skip", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "keep.txt").write_text("keep", encoding="utf-8")
    await _publish(client, token, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    data = listed.json()["data"]
    assert [item["path"] for item in data] == ["outputs/keep.txt"]


async def test_artifact_content_gone_if_never_published(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    session_id, _directory = await _hosted_session(client, token)
    async with store.session() as db:
        row = await db.scalar(
            select(SessionRow).where(SessionRow.id == uuid.UUID(session_id))
        )
        assert row is not None
        artifact = await create_artifact(
            db, row.tenant_id, row.id, path="artifacts/note.txt"
        )
        artifact_id = str(artifact.id)

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 410
    assert content.json()["error"]["code"] == "gone"


async def test_delete_artifact_removes_file_and_metadata(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    artifact_id = listed.json()["data"][0]["id"]

    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(token),
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": artifact_id, "deleted": True}

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.json() == {"data": []}
    missing = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert missing.status_code == 404


async def test_artifacts_unknown_session_is_404(client: AsyncClient) -> None:
    token = _token()
    missing = uuid.uuid4()
    listed = await client.get(
        f"/v1/agents/sessions/{missing}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 404
    content = await client.get(
        f"/v1/agents/sessions/{missing}/artifacts/{missing}/content",
        headers=_auth(token),
    )
    assert content.status_code == 404
    deleted = await client.delete(
        f"/v1/agents/sessions/{missing}/artifacts/{missing}",
        headers=_auth(token),
    )
    assert deleted.status_code == 404


async def test_cross_tenant_artifacts_are_404(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id, directory = await _hosted_session(client, token_a)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token_a, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token_a)
    )
    artifact_id = listed.json()["data"][0]["id"]

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token_b)
    )
    assert listed.status_code == 404
    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token_b),
    )
    assert content.status_code == 404
    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(token_b),
    )
    assert deleted.status_code == 404
    content_a = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token_a),
    )
    assert content_a.status_code == 200
    assert content_a.content == b"hello"


async def test_workspace_persists_after_harvest(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "keep.txt").write_text("stay", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token, session_id)
    assert directory.exists()
    assert (directory / "keep.txt").read_text(encoding="utf-8") == "stay"
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.json()["data"][0]["path"] == "outputs/note.txt"


async def test_publish_on_turn_complete(client: AsyncClient) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("hello", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "out.bin").write_bytes(b"xyz")
    turned = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "done"},
    )
    assert turned.status_code == 200
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    data = listed.json()["data"]
    by_path = {item["path"]: item for item in data}
    assert set(by_path) == {"outputs/out.bin"}
    assert by_path["outputs/out.bin"]["turn_id"] is not None
    assert by_path["outputs/out.bin"]["content_type"] == "application/octet-stream"
    assert directory.exists()
    note = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/"
        f"{by_path['outputs/out.bin']['id']}/content",
        headers=_auth(token),
    )
    assert note.content == b"xyz"


async def test_later_turn_publishes_new_artifact_for_same_path(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("one", encoding="utf-8")
    await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "first"},
    )
    (directory / "outputs" / "note.txt").write_text("two", encoding="utf-8")
    await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "second"},
    )
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    data = listed.json()["data"]
    assert [item["path"] for item in data] == [
        "outputs/note.txt",
        "outputs/note.txt",
    ]
    assert data[0]["id"] != data[1]["id"]
    first = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{data[0]['id']}/content",
        headers=_auth(token),
    )
    second = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{data[1]['id']}/content",
        headers=_auth(token),
    )
    assert first.content == b"one"
    assert second.content == b"two"


async def test_sandbox_ttl_openai_hosted_wipes_dir_keeps_artifacts(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        worker,
    ):
        token = _token()
        session_id, directory = await _hosted_session(client, token)
        (directory / "keep.txt").write_text("stay", encoding="utf-8")
        (directory / "outputs").mkdir()
        (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
        await _publish(client, token, session_id)
        await reap_workspaces(
            worker.execution.settings,
            worker.execution.pool,
            ttl_overrides=worker.execution._context_ttl,
            now=utc_now() + timedelta(hours=2),
        )
        assert not directory.exists()
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        artifact_id = listed.json()["data"][0]["id"]
        content = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
            headers=_auth(token),
        )
        assert content.content == b"hello"
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        assert events.json()["data"]


async def test_delete_session_removes_workspace_and_artifacts(
    client: AsyncClient,
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "keep.txt").write_text("stay", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _publish(client, token, session_id)
    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": session_id, "deleted": True}
    assert not directory.exists()
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 404


async def test_artifact_cap_rejects_publish(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    limited = settings.model_copy(update={"max_artifact_bytes": 16})
    async with split_client_for(limited, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "disk-art"
        session_id, directory = await _hosted_session(client, token)
        (directory / "outputs").mkdir()
        (directory / "outputs" / "big.bin").write_bytes(b"x" * 64)
        await _publish(client, token, session_id)
        events = await _events(client, token, session_id)
        codes = [
            event["data"]["code"]
            for event in events
            if event["type"] == "agent.session.error"
        ]
        assert codes == ["artifact_too_large"]
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        assert listed.status_code == 200
        assert listed.json()["data"] == []


async def test_publish_unwritable_store_is_artifact_store(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "disk-perm"
        session_id, directory = await _hosted_session(client, token)
        (directory / "outputs").mkdir()
        (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
        artifacts = store_root(settings) / ".artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        artifacts.chmod(0o500)
        try:
            await _publish(client, token, session_id)
        finally:
            artifacts.chmod(0o755)
        events = await _events(client, token, session_id)
        assert "agent.session.turn.failed" in [event["type"] for event in events]
        codes = [
            event["data"]["code"]
            for event in events
            if event["type"] == "agent.session.error"
        ]
        assert codes == ["artifact_store"]


async def test_artifact_cap_allows_under_limit(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    limited = settings.model_copy(update={"max_artifact_bytes": 64})
    async with split_client_for(limited, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "disk-ok"
        session_id, directory = await _hosted_session(client, token)
        (directory / "outputs").mkdir()
        (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
        await _publish(client, token, session_id)
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        assert listed.status_code == 200
        assert listed.json()["data"][0]["path"] == "outputs/note.txt"


async def test_workspace_cap_emits_error_and_can_still_publish(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    limited = settings.model_copy(
        update={"max_workspace_bytes": 16, "max_artifact_bytes": 1024}
    )
    async with split_client_for(limited, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "disk-ws"
        session_id, directory = await _hosted_session(client, token)
        (directory / "scratch.bin").write_bytes(b"x" * 64)
        (directory / "outputs").mkdir()
        (directory / "outputs" / "note.txt").write_text("hi", encoding="utf-8")
        await _publish(client, token, session_id)
        events = await _events(client, token, session_id)
        codes = [
            event["data"]["code"]
            for event in events
            if event["type"] == "agent.session.error"
        ]
        assert codes == ["workspace_too_large"]
        listed = await client.get(
            f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
        )
        assert listed.status_code == 200
        assert listed.json()["data"][0]["path"] == "outputs/note.txt"
