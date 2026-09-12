import uuid
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select

from apipi.config import Settings
from apipi.pi.artifacts import harvest_session
from apipi.store.engine import Store
from apipi.store.models import SessionRow
from apipi.store.repo import create_artifact


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
        await harvest_session(db, settings, uuid.UUID(session_id), None)


async def test_write_host_file_and_fetch_content(
    client: AsyncClient, store: Store, settings: Settings
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("hello", encoding="utf-8")
    await _harvest(store, settings, session_id)
    assert not directory.exists()

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["session_id"] == session_id
    assert data[0]["path"] == "artifacts/note.txt"
    assert data[0]["content_type"] == "text/plain"
    artifact_id = data[0]["id"]

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    assert content.content == b"hello"
    assert content.headers["content-type"].startswith("text/plain")


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
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("hello", encoding="utf-8")
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
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "note.txt").write_text("hello", encoding="utf-8")
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
