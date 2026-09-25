from pathlib import Path

from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def test_files_crud_and_session_attach(client: AsyncClient) -> None:
    token = "files-crud"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": ("amounts.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    file_id = body["id"]
    assert file_id.startswith("file-")
    assert body["object"] == "file"
    assert body["bytes"] == 8
    assert body["filename"] == "amounts.csv"
    assert body["purpose"] == "user_data"
    assert body["status"] == "processed"
    assert isinstance(body["created_at"], int)
    listed = await client.get("/v1/files", headers=_auth(token))
    assert listed.json()["object"] == "list"
    assert listed.json()["data"][0]["id"] == file_id
    got = await client.get(f"/v1/files/{file_id}", headers=_auth(token))
    assert got.json()["id"] == file_id
    content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    assert content.status_code == 200
    assert content.content == b"a,b\n1,2\n"
    other = await client.get(f"/v1/files/{file_id}", headers=_auth("other-tenant"))
    assert other.status_code == 404
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "file_id",
                        "file_id": file_id,
                        "path": "/workspace/amounts.csv",
                    }
                ],
            },
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["files"][0]["file_id"] == file_id
    directory = Path(env["directory"])
    assert (directory / "amounts.csv").read_bytes() == b"a,b\n1,2\n"


async def test_file_id_missing_is_not_found(client: AsyncClient) -> None:
    token = "files-missing"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "file_id",
                        "file_id": "file-missing",
                        "path": "/workspace/x.txt",
                    }
                ],
            },
        },
    )
    assert response.status_code == 404


async def test_file_purpose_not_implemented(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/files",
        headers=_auth("files-purpose"),
        data={"purpose": "fine-tune"},
        files={"file": ("data.jsonl", b"{}", "application/json")},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "not_implemented"
    assert error["code"] == "fine-tune"


async def test_delete_file_then_attach_is_not_found(client: AsyncClient) -> None:
    token = "files-delete"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "assistants"},
        files={"file": ("note.txt", b"hi", "text/plain")},
    )
    file_id = uploaded.json()["id"]
    deleted = await client.delete(f"/v1/files/{file_id}", headers=_auth(token))
    assert deleted.json()["deleted"] is True
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
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
    assert response.status_code == 404


async def test_file_content_disposition_non_ascii(client: AsyncClient) -> None:
    token = "files-unicode"
    name = "Bericht_Größe_✓.pdf"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": (name, b"%PDF", "application/pdf")},
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["id"]
    content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    assert content.status_code == 200
    disposition = content.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert content.headers["x-content-type-options"] == "nosniff"
