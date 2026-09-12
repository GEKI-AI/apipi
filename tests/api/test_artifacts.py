import uuid
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import select

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


async def _add_artifact(
    store: Store, session_id: str, path: str, content_type: str = "text/plain"
) -> str:
    async with store.session() as db:
        row = await db.scalar(
            select(SessionRow).where(SessionRow.id == uuid.UUID(session_id))
        )
        assert row is not None
        artifact = await create_artifact(
            db, row.tenant_id, row.id, path=path, content_type=content_type
        )
        return str(artifact.id)


async def test_write_host_file_and_fetch_content(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    (directory / "note.txt").write_text("hello", encoding="utf-8")
    artifact_id = await _add_artifact(store, session_id, "note.txt")

    listed = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts", headers=_auth(token)
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == artifact_id
    assert data[0]["session_id"] == session_id
    assert data[0]["path"] == "note.txt"
    assert data[0]["content_type"] == "text/plain"

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 200
    assert content.content == b"hello"
    assert content.headers["content-type"].startswith("text/plain")


async def test_artifact_content_gone_if_file_missing(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    target = directory / "note.txt"
    target.write_text("hello", encoding="utf-8")
    artifact_id = await _add_artifact(store, session_id, "note.txt")
    target.unlink()

    content = await client.get(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        headers=_auth(token),
    )
    assert content.status_code == 410
    assert content.json()["error"]["code"] == "gone"


async def test_delete_artifact_removes_file_and_metadata(
    client: AsyncClient, store: Store
) -> None:
    token = _token()
    session_id, directory = await _hosted_session(client, token)
    target = directory / "note.txt"
    target.write_text("hello", encoding="utf-8")
    artifact_id = await _add_artifact(store, session_id, "note.txt")

    deleted = await client.delete(
        f"/v1/agents/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(token),
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": artifact_id, "deleted": True}
    assert not target.exists()

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
    client: AsyncClient, store: Store
) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id, directory = await _hosted_session(client, token_a)
    (directory / "note.txt").write_text("hello", encoding="utf-8")
    artifact_id = await _add_artifact(store, session_id, "note.txt")

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
    assert (directory / "note.txt").read_text(encoding="utf-8") == "hello"
