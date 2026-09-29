from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_update_and_running_session_keeps_version(
    client: AsyncClient,
) -> None:
    token = "versions"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "instructions": "be brief"},
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    assert created.json()["active_version"]["number"] == 1
    listed = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["number"] == 1
    assert "definition" not in listed.json()["data"][0]
    same = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "be brief"},
    )
    assert same.status_code == 200
    assert same.json()["active_version"]["number"] == 1
    edited = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "be long"},
    )
    assert edited.status_code == 200
    assert edited.json()["active_version"]["number"] == 2
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert session.status_code == 200
    assert session.json()["agent_version"]["number"] == 2
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "be shorter"},
    )
    follow = await client.post(
        f"/v1/agents/sessions/{session.json()['id']}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    harness = client._transport.app.state.gateway.harness
    assert harness.instructions is not None
    assert "be long" in harness.instructions
    assert "be shorter" not in harness.instructions
    got = await client.get(
        f"/v1/agents/sessions/{session.json()['id']}", headers=_auth(token)
    )
    assert got.json()["agent_version"]["number"] == 2


async def test_version_stores_ids_not_vault_token(client: AsyncClient) -> None:
    token = "version-secret"
    vault = await client.post(
        "/v1/agents/vaults",
        headers=_auth(token),
        json={"name": "box"},
    )
    vault_id = vault.json()["id"]
    cred = await client.post(
        f"/v1/agents/vaults/{vault_id}/credentials",
        headers=_auth(token),
        json={
            "name": "pat",
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://mcp.example.com/mcp",
                "token": "secret-token",
            },
        },
    )
    assert cred.status_code == 200
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": {"vault_ids": [vault_id]},
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "box",
                    "transport": {
                        "type": "http",
                        "server_url": "https://mcp.example.com/mcp",
                    },
                    "credential_id": cred.json()["id"],
                }
            ],
        },
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    got = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions/1", headers=_auth(token)
    )
    text = got.text
    assert "secret-token" not in text
    assert cred.json()["id"] in text
    assert vault_id in text


async def test_pin_activate_and_refuse_delete(client: AsyncClient) -> None:
    token = "pin-version"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "instructions": "one"},
    )
    agent_id = created.json()["id"]
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "two"},
    )
    rolled = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions/1/activate",
        headers=_auth(token),
    )
    assert rolled.status_code == 200
    assert rolled.json()["active"] is True
    assert rolled.json()["number"] == 1
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "metadata": {"apipi.agent_version": 2},
            "environment": {"type": "none"},
        },
    )
    assert session.status_code == 200
    assert session.json()["agent_version"]["number"] == 2
    refused = await client.delete(
        f"/v1/apipi/agents/{agent_id}/versions/2", headers=_auth(token)
    )
    assert refused.status_code == 400
    other = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions/1", headers=_auth("other-tenant")
    )
    assert other.status_code == 404
