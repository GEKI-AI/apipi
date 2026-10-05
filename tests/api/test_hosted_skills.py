from httpx import AsyncClient
from tests.support.files import zip_skill
from tests.support.http import auth, create_agent

from apipi.common.skills import discover_skill_dirs


async def test_skill_upload_and_session_attach(client: AsyncClient) -> None:
    token = "skill-crud"
    uploaded = await client.post(
        "/v1/skills",
        headers=auth(token),
        files={
            "files": (
                "demo.zip",
                zip_skill("demo", {"scripts/run.sh": "echo ok\n"}),
                "application/zip",
            )
        },
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    skill_id = body["id"]
    assert skill_id.startswith("skill-")
    assert body["object"] == "skill"
    assert body["name"] == "demo"
    listed = await client.get("/v1/skills", headers=auth(token))
    assert listed.json()["data"][0]["id"] == skill_id
    other = await client.get(f"/v1/skills/{skill_id}", headers=auth("other-tenant"))
    assert other.status_code == 404
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
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
    from typing import Any, cast

    from httpx import ASGITransport
    from tests.support.workspace import hosted_dir

    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = cast(Any, transport.app)
    settings = app.state.gateway.settings
    directory = hosted_dir(settings, token, created.json()["id"])
    skill_md = directory / ".agents" / "skills" / "demo" / "SKILL.md"
    assert skill_md.is_file()
    trees = discover_skill_dirs(directory)
    assert str(skill_md.parent.resolve()) in trees


async def test_skill_missing_is_not_found(client: AsyncClient) -> None:
    token = "skill-missing"
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
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
        headers=auth("skill-bad"),
        files={"files": ("nope.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_skill_unknown_type_not_implemented(client: AsyncClient) -> None:
    token = "skill-type"
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
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
        headers=auth(token),
        files={
            "files": (
                "demo.zip",
                zip_skill("gone", {"scripts/run.sh": "echo ok\n"}),
                "application/zip",
            )
        },
    )
    skill_id = uploaded.json()["id"]
    deleted = await client.delete(f"/v1/skills/{skill_id}", headers=auth(token))
    assert deleted.json()["deleted"] is True
    agent_id = await create_agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "skills": [{"type": "skill_reference", "skill_id": skill_id}],
            },
        },
    )
    assert response.status_code == 404
