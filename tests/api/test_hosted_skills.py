import io
import zipfile
from pathlib import Path

from httpx import AsyncClient

from apipi.services.skills import discover_skill_dirs


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _zip_skill(name: str, *, body: str | None = None) -> bytes:
    text = body if body is not None else f"---\nname: {name}\n---\nDo the thing.\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", text)
        archive.writestr(f"{name}/scripts/run.sh", "echo ok\n")
    return buf.getvalue()


async def _agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def test_skill_upload_and_session_attach(client: AsyncClient) -> None:
    token = "skill-crud"
    uploaded = await client.post(
        "/v1/skills",
        headers=_auth(token),
        files={"files": ("demo.zip", _zip_skill("demo"), "application/zip")},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    skill_id = body["id"]
    assert skill_id.startswith("skill-")
    assert body["object"] == "skill"
    assert body["name"] == "demo"
    listed = await client.get("/v1/skills", headers=_auth(token))
    assert listed.json()["data"][0]["id"] == skill_id
    other = await client.get(f"/v1/skills/{skill_id}", headers=_auth("other-tenant"))
    assert other.status_code == 404
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "skills": [{"type": "skill_reference", "skill_id": skill_id}],
            },
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["skills"][0]["skill_id"] == skill_id
    directory = Path(env["directory"])
    skill_md = directory / ".agents" / "skills" / "demo" / "SKILL.md"
    assert skill_md.is_file()
    trees = discover_skill_dirs(directory)
    assert str(skill_md.parent.resolve()) in trees


async def test_skill_missing_is_not_found(client: AsyncClient) -> None:
    token = "skill-missing"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "skills": [{"type": "skill_reference", "skill_id": "skill-missing"}],
            },
        },
    )
    assert response.status_code == 404


async def test_skill_bad_zip_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/skills",
        headers=_auth("skill-bad"),
        files={"files": ("nope.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_skill_unknown_type_not_implemented(client: AsyncClient) -> None:
    token = "skill-type"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "skills": [{"type": "inline", "name": "x"}],
            },
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "not_implemented"
    assert error["code"] == "skills"


async def test_delete_skill_then_attach_is_not_found(client: AsyncClient) -> None:
    token = "skill-delete"
    uploaded = await client.post(
        "/v1/skills",
        headers=_auth(token),
        files={"files": ("demo.zip", _zip_skill("gone"), "application/zip")},
    )
    skill_id = uploaded.json()["id"]
    deleted = await client.delete(f"/v1/skills/{skill_id}", headers=_auth(token))
    assert deleted.json()["deleted"] is True
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "skills": [{"type": "skill_reference", "skill_id": skill_id}],
            },
        },
    )
    assert response.status_code == 404
