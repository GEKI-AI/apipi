import io
import json
import zipfile

from httpx import AsyncClient

from apipi.services.bundles import comparable_manifest


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _zip_skill(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\n---\nDo the thing.\n")
    return buf.getvalue()


async def _skill(client: AsyncClient, token: str) -> str:
    uploaded = await client.post(
        "/v1/skills",
        headers=_auth(token),
        files={"files": ("demo.zip", _zip_skill("demo"), "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return str(uploaded.json()["id"])


async def _file(client: AsyncClient, token: str) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        files={"file": ("note.txt", b"hello-file", "text/plain")},
        data={"purpose": "user_data"},
    )
    assert uploaded.status_code == 200, uploaded.text
    return str(uploaded.json()["id"])


def _manifest(data: bytes) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return json.loads(archive.read("agent.json"))


async def test_template_round_trip_hides_secrets(client: AsyncClient) -> None:
    token = "tpl-round"
    skill_id = await _skill(client, token)
    file_id = await _file(client, token)
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "Research",
            "model": "test",
            "instructions": "look it up",
            "idle_ttl": "30m",
            "reasoning": {"effort": "medium"},
            "metadata": {
                "apipi.system_prompt": "be brief",
                "host.keep": "yes",
                "apipi.title": "drop-me",
            },
            "tools": [
                {"type": "function", "name": "lookup", "parameters": {}},
                {
                    "type": "mcp",
                    "server_label": "search",
                    "server_url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "secret-token"},
                },
            ],
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "sandbox_size": "M",
                    "sandbox_image": "default",
                    "env": {"LOG_LEVEL": "info", "SERVICE_TOKEN": "secret-env"},
                    "files": [
                        {
                            "type": "file_id",
                            "file_id": file_id,
                            "path": "data/note.txt",
                        }
                    ],
                    "skills": [{"type": "skill_reference", "skill_id": skill_id}],
                }
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    stored = await client.post(
        "/v1/apipi/templates",
        headers=_auth(token),
        json={"agent_id": agent_id, "name": "Research agent"},
    )
    assert stored.status_code == 200, stored.text
    assert "dropped metadata apipi.title" in stored.json()["warnings"]
    template_id = stored.json()["id"]
    assert stored.json()["created_by"] is None
    assert stored.json()["visibility"] == "tenant"
    downloaded = await client.get(
        f"/v1/apipi/templates/{template_id}/download", headers=_auth(token)
    )
    assert downloaded.status_code == 200
    assert b"secret-token" not in downloaded.content
    assert b"secret-env" not in downloaded.content
    manifest = _manifest(downloaded.content)
    agent = manifest["agent"]
    assert isinstance(agent, dict)
    metadata = agent["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["host.keep"] == "yes"
    assert "apipi.title" not in metadata
    imported = await client.post(
        "/v1/apipi/templates/import",
        headers=_auth(token),
        files={
            "bundle": (
                "agent.apipi-agent.zip",
                downloaded.content,
                "application/zip",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    made = await client.post(
        f"/v1/apipi/templates/{imported.json()['id']}/agents",
        headers=_auth(token),
        json={
            "secrets": {
                "SEARCH_AUTHORIZATION": "secret-token",
                "LOG_LEVEL": "info",
                "SERVICE_TOKEN": "secret-env",
            }
        },
    )
    assert made.status_code == 200, made.text
    assert made.json()["missing"]["secrets"] == []
    new_id = made.json()["agent"]["id"]
    assert new_id != agent_id
    assert made.json()["skills"][0]["id"] != skill_id
    assert (
        made.json()["agent"]["metadata"]["apipi.template_id"] == imported.json()["id"]
    )
    exported = await client.get(
        f"/v1/apipi/agents/{new_id}/export", headers=_auth(token)
    )
    assert exported.status_code == 200
    assert b"secret-token" not in exported.content
    assert b"secret-env" not in exported.content
    again = comparable_manifest(_manifest(exported.content))
    first = comparable_manifest(manifest)
    assert again["agent"]["name"] == first["agent"]["name"]
    assert again["agent"]["metadata"]["apipi.thinking"] == "medium"
    assert "apipi.template_id" not in again["agent"].get("metadata", {})
    deleted = await client.delete(
        f"/v1/apipi/templates/{template_id}", headers=_auth(token)
    )
    assert deleted.status_code == 200
    still = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert still.status_code == 200
    other = await client.get(
        f"/v1/apipi/templates/{imported.json()['id']}", headers=_auth("tpl-other")
    )
    assert other.status_code == 404


async def test_import_rejects_bad_zip(client: AsyncClient) -> None:
    token = "tpl-bad"
    uploaded = await client.post(
        "/v1/apipi/templates/import",
        headers=_auth(token),
        files={"bundle": ("nope.zip", b"not-a-zip", "application/zip")},
    )
    assert uploaded.status_code == 400
    listed = await client.get("/v1/apipi/templates", headers=_auth(token))
    assert listed.json()["data"] == []
