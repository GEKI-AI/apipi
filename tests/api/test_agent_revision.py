import uuid

from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_revision_increments_on_update_and_restore_not_snapshot(
    client: AsyncClient,
) -> None:
    token = "rev-key"
    created = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot"}
    )
    assert created.status_code == 200
    assert created.json()["revision"] == 1
    agent_id = created.json()["id"]

    updated = await client.post(
        f"/v1/agents/{agent_id}", headers=_auth(token), json={"name": "bot2"}
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2

    read = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert read.json()["revision"] == 2

    snap = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions",
        headers=_auth(token),
        json={"name": "v1"},
    )
    assert snap.status_code == 200
    assert (await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))).json()[
        "revision"
    ] == 2

    restored = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions/1/restore", headers=_auth(token)
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 3

    deleted = await client.delete(
        f"/v1/apipi/agents/{agent_id}/versions/1", headers=_auth(token)
    )
    assert deleted.status_code == 200
    assert (await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))).json()[
        "revision"
    ] == 3


async def test_revision_is_read_only(client: AsyncClient) -> None:
    token = "rev-ro"
    bad = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "revision": 5}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "unknown_field"
    ok = await client.post("/v1/agents", headers=_auth(token), json={"name": "bot"})
    agent_id = ok.json()["id"]
    bad_update = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"name": "x", "revision": 9},
    )
    assert bad_update.status_code == 400
    assert bad_update.json()["error"]["code"] == "unknown_field"
    assert str(uuid.UUID(ok.json()["id"])) == ok.json()["id"]
