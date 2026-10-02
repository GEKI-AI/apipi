import io
import uuid
import zipfile

from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _zip_skill(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\n---\nDo the thing.\n")
    return buf.getvalue()


async def _skill(client: AsyncClient, token: str, name: str = "demo") -> str:
    uploaded = await client.post(
        "/v1/skills",
        headers=_auth(token),
        files={"files": (f"{name}.zip", _zip_skill(name), "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return str(uploaded.json()["id"])


async def _file(client: AsyncClient, token: str) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"purpose": "user_data"},
    )
    assert uploaded.status_code == 200, uploaded.text
    return str(uploaded.json()["id"])


async def _vault(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents/vaults",
        headers=_auth(token),
        json={"name": "v"},
    )
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


def _defaults(skill_id: str, file_id: str, vault_id: str) -> dict[str, object]:
    return {
        "environment": {
            "type": "openai_hosted",
            "env": {"LOG_LEVEL": "info", "A": "agent"},
            "packages": {"python": ["httpx"]},
            "sandbox_size": "M",
            "sandbox_image": "default",
            "files": [{"type": "file_id", "file_id": file_id, "path": "data/note.txt"}],
            "skills": [{"type": "skill_reference", "skill_id": skill_id}],
        },
        "vault_ids": [vault_id],
    }


async def test_agent_session_defaults_round_trip(client: AsyncClient) -> None:
    token = "defaults-round"
    skill_id = await _skill(client, token)
    file_id = await _file(client, token)
    vault_id = await _vault(client, token)
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": _defaults(skill_id, file_id, vault_id),
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["session_defaults"]["environment"]["env"]["LOG_LEVEL"] == "info"
    assert body["session_defaults"]["vault_ids"] == [vault_id]
    assert "apipi.sandbox_size" not in body["metadata"]
    agent_id = body["id"]
    cleared = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"session_defaults": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["session_defaults"] is None
    assert "apipi.sandbox_size" not in cleared.json()["metadata"]


async def test_session_uses_and_overrides_defaults(client: AsyncClient) -> None:
    token = "defaults-merge"
    skill_id = await _skill(client, token, "one")
    extra = await _skill(client, token, "two")
    file_id = await _file(client, token)
    vault_id = await _vault(client, token)
    other = await _vault(client, token)
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": _defaults(skill_id, file_id, vault_id),
        },
    )
    agent_id = created.json()["id"]
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "env": {"A": "session"},
                "skills": [{"type": "skill_reference", "skill_id": extra}],
            },
            "vault_ids": [other],
        },
    )
    assert session.status_code == 200, session.text
    env = session.json()["environment"]
    assert env["env"]["LOG_LEVEL"] == "info"
    assert env["env"]["A"] == "session"
    assert env["packages"] == {"python": ["httpx"]}
    assert env["sandbox_size"] == "M"
    skill_ids = [item["skill_id"] for item in env["skills"]]
    assert skill_ids == [skill_id, extra]
    assert session.json()["vault_ids"] == [vault_id, other]


async def test_inherit_false_and_chat_type_rule(client: AsyncClient) -> None:
    token = "defaults-opt-out"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.sandbox_size": "M"},
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "env": {"SECRET": "nope"},
                    "sandbox_size": "M",
                }
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    assert created.json()["session_defaults"]["environment"]["sandbox_size"] == "M"
    opted = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "inherit_agent_defaults": False,
            "environment": {"type": "openai_hosted"},
        },
    )
    assert opted.status_code == 200, opted.text
    assert "env" not in opted.json()["environment"]
    assert opted.json()["environment"]["sandbox_size"] == "S"
    chat = await client.post(
        "/v1/apipi/chat/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert chat.status_code == 200, chat.text


async def test_unknown_and_dangling_refs(client: AsyncClient) -> None:
    token = "defaults-refs"
    missing = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "skills": [
                        {"type": "skill_reference", "skill_id": "skill_missing"}
                    ],
                }
            },
        },
    )
    assert missing.status_code == 404
    skill_id = await _skill(client, token)
    other = await client.post(
        "/v1/agents",
        headers=_auth("defaults-other"),
        json={
            "name": "bot",
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "skills": [{"type": "skill_reference", "skill_id": skill_id}],
                }
            },
        },
    )
    assert other.status_code == 404
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "skills": [{"type": "skill_reference", "skill_id": skill_id}],
                }
            },
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    deleted = await client.delete(f"/v1/skills/{skill_id}", headers=_auth(token))
    assert deleted.status_code == 200
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert session.status_code == 400
    message = session.json()["error"]["message"]
    assert agent_id in message
    assert skill_id in message


async def test_alias_conflict_and_hosted_only(client: AsyncClient) -> None:
    token = "defaults-alias"
    conflict = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "metadata": {"apipi.sandbox_size": "S"},
            "session_defaults": {
                "environment": {"type": "openai_hosted", "sandbox_size": "L"}
            },
        },
    )
    assert conflict.status_code == 200, conflict.text
    assert conflict.json()["session_defaults"]["environment"]["sandbox_size"] == "L"
    hosted = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "session_defaults": {"environment": {"type": "none", "env": {"A": "1"}}},
        },
    )
    assert hosted.status_code == 400
    inline = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent": {
                "name": "inline",
                "model": "test",
                "session_defaults": {
                    "environment": {
                        "type": "openai_hosted",
                        "env": {"FROM": "inline"},
                    }
                },
            }
        },
    )
    assert inline.status_code == 200, inline.text
    assert inline.json()["environment"]["env"]["FROM"] == "inline"
    assert inline.json()["agent_id"] is None


async def test_session_field_beats_agent_sandbox(client: AsyncClient) -> None:
    token = "defaults-size"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "sandbox_size": "M",
                    "sandbox_image": "default",
                }
            },
        },
    )
    agent_id = created.json()["id"]
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "metadata": {"apipi.sandbox_size": "S"},
        },
    )
    assert session.status_code == 200, session.text
    assert session.json()["environment"]["sandbox_size"] == "M"
    assert uuid.UUID(agent_id)
