import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from apipi.config import DiskLimitError, Settings
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import create_artifact, get_session_by_id
from apipi.worker.pi.artifacts import harvest_session, reap_workspaces
from apipi.worker.pi.pool import PiPool


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
    directory = Path(created.json()["environment"]["directory"])
    return str(created.json()["id"]), directory


async def _harvest(store: Store, settings: Settings, session_id: str) -> None:
    async with store.session() as db:
        _row, error = await harvest_session(db, settings, uuid.UUID(session_id), None)
    if error is not None:
        raise error


async def test_write_host_file_and_fetch_content(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
    assert directory.exists()

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["session_id"] == session_id
    assert data[0]["path"] == "outputs/note.txt"
    assert data[0]["content_type"] == "text/plain"
    artifact_id = data[0]["id"]

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    assert content.content == b"hello"
    assert content.headers["content-type"].startswith("text/plain")


async def test_harvest_skips_workspace_artifacts_folder(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("skip", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "keep.txt").write_text("keep", encoding="utf-8")
    await _harvest(store, settings, session_id)
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
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
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


async def test_cross_tenant_artifacts_are_404(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id, directory = await _hosted_session(client, token_a)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
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
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "keep.txt").write_text("stay", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
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


async def test_workspace_ttl_wipes_dir_keeps_artifacts(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "keep.txt").write_text("stay", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
    async with store.session() as db:
        row = await get_session_by_id(db, uuid.UUID(session_id))
        assert row is not None
        row.updated_at = utc_now() - timedelta(hours=2)
    await reap_workspaces(settings, store, PiPool(settings))
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
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "keep.txt").write_text("stay", encoding="utf-8")
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
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
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = "disk-art"
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "big.bin").write_bytes(b"x" * 64)
    limited = settings.model_copy(update={"max_artifact_bytes": 16})
    with pytest.raises(DiskLimitError) as exc:
        await _harvest(store, limited, session_id)
    assert exc.value.code == "artifact_too_large"
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == []


async def test_artifact_cap_allows_under_limit(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = "disk-ok"
    session_id, directory = await _hosted_session(client, token)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hello", encoding="utf-8")
    limited = settings.model_copy(update={"max_artifact_bytes": 64})
    await _harvest(store, limited, session_id)
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["path"] == "outputs/note.txt"


async def test_workspace_cap_emits_error_and_can_still_publish(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = "disk-ws"
    session_id, directory = await _hosted_session(client, token)
    (directory / "scratch.bin").write_bytes(b"x" * 64)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "note.txt").write_text("hi", encoding="utf-8")
    limited = settings.model_copy(
        update={"max_workspace_bytes": 16, "max_artifact_bytes": 1024}
    )
    with pytest.raises(DiskLimitError) as exc:
        await _harvest(store, limited, session_id)
    assert exc.value.code == "workspace_too_large"
    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["path"] == "outputs/note.txt"
