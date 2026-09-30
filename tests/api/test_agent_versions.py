import io
import json
import zipfile

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _listen(store: Store) -> list[str]:
    seen: list[str] = []

    def _capture(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(store.engine.sync_engine, "before_cursor_execute", _capture)
    return seen


async def test_normal_flow_does_not_touch_versions(
    client: AsyncClient, store: Store
) -> None:
    seen = _listen(store)
    token = "no-versions"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "instructions": "one"},
    )
    assert created.status_code == 200
    assert "active_version" not in created.json()
    agent_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "two"},
    )
    assert updated.status_code == 200
    assert "active_version" not in updated.json()
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {"type": "none"},
            "metadata": {"apipi.agent_version": 9, "team": "x"},
            "input": "hello",
        },
    )
    assert session.status_code == 200
    body = session.json()
    assert "agent_version" not in body
    assert body["metadata"]["apipi.agent_version"] == 9
    moved = await client.post(
        f"/v1/agents/sessions/{body['id']}",
        headers=_auth(token),
        json={"metadata": {"apipi.agent_version": "nope", "team": "y"}},
    )
    assert moved.status_code == 200
    assert moved.json()["metadata"]["apipi.agent_version"] == "nope"
    assert "agent_version" not in moved.json()
    follow = await client.post(
        f"/v1/agents/sessions/{body['id']}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    spare = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "spare", "model": "test"},
    )
    deleted = await client.delete(
        f"/v1/agents/{spare.json()['id']}", headers=_auth(token)
    )
    assert deleted.status_code == 200
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.status_code == 200
    assert not any("agent_versions" in statement.lower() for statement in seen)


async def test_edit_changes_the_next_turn(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    token = "live-edit"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test", "instructions": "be long"},
        )
        agent_id = created.json()["id"]
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
    assert harness.instructions is not None
    assert "be shorter" in harness.instructions


async def test_snapshot_restore_and_never_reuse_numbers(client: AsyncClient) -> None:
    token = "snapshots"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "instructions": "one"},
    )
    agent_id = created.json()["id"]
    first = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions",
        headers=_auth(token),
        json={"name": "start", "comment": "first"},
    )
    assert first.status_code == 200
    assert first.json()["number"] == 1
    assert first.json()["name"] == "start"
    assert first.json()["source"] == "explicit"
    assert first.json()["definition"]["instructions"] == "one"
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "two"},
    )
    removed = await client.delete(
        f"/v1/apipi/agents/{agent_id}/versions/1", headers=_auth(token)
    )
    assert removed.status_code == 200
    second = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token), json={}
    )
    assert second.status_code == 200
    assert second.json()["number"] == 2
    listed = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token)
    )
    assert [row["number"] for row in listed.json()["data"]] == [2]
    assert "definition" not in listed.json()["data"][0]
    got = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions/2", headers=_auth(token)
    )
    assert got.json()["definition"]["instructions"] == "two"
    other = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions/2", headers=_auth("other-tenant")
    )
    assert other.status_code == 404
    restored = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions/2/restore", headers=_auth(token)
    )
    assert restored.status_code == 200
    assert restored.json()["instructions"] == "two"
    pre = restored.json()["pre_restore_version"]["number"]
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "three"},
    )
    back = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions/{pre}/restore",
        headers=_auth(token),
    )
    assert back.status_code == 200
    assert back.json()["instructions"] == "two"


async def test_restore_missing_credential_changes_nothing(client: AsyncClient) -> None:
    token = "missing-cred"
    vault = await client.post(
        "/v1/agents/vaults", headers=_auth(token), json={"name": "box"}
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
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "instructions": "keep",
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "box",
                    "server_url": "https://mcp.example.com/mcp",
                    "credential_id": cred.json()["id"],
                }
            ],
        },
    )
    agent_id = created.json()["id"]
    snap = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token), json={}
    )
    assert "secret-token" not in snap.text
    await client.delete(
        f"/v1/agents/vaults/{vault_id}/credentials/{cred.json()['id']}",
        headers=_auth(token),
    )
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "changed"},
    )
    refused = await client.post(
        f"/v1/apipi/agents/{agent_id}/versions/1/restore", headers=_auth(token)
    )
    assert refused.status_code == 400
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.json()["instructions"] == "changed"
    listed = await client.get(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token)
    )
    assert [row["number"] for row in listed.json()["data"]] == [1]


async def test_retention_counts_pre_restore(settings: Settings, store: Store) -> None:
    app = create_app(
        settings.model_copy(update={"agent_versions_keep": 2}),
        store=store,
        harness=FakeHarness(),
    )
    token = "keep-two"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test", "instructions": "v0"},
        )
        agent_id = created.json()["id"]
        for text in ("one", "two", "three"):
            await client.post(
                f"/v1/agents/{agent_id}",
                headers=_auth(token),
                json={"instructions": text},
            )
            posted = await client.post(
                f"/v1/apipi/agents/{agent_id}/versions",
                headers=_auth(token),
                json={},
            )
            assert posted.status_code == 200
        listed = await client.get(
            f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token)
        )
        assert [row["number"] for row in listed.json()["data"]] == [3, 2]
        restored = await client.post(
            f"/v1/apipi/agents/{agent_id}/versions/2/restore",
            headers=_auth(token),
        )
        assert restored.status_code == 200
        assert restored.json()["instructions"] == "two"
        after = await client.get(
            f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token)
        )
        numbers = [row["number"] for row in after.json()["data"]]
        assert 2 not in numbers
        assert restored.json()["pre_restore_version"]["number"] in numbers


async def test_export_version_and_template_creates_none(client: AsyncClient) -> None:
    token = "export-version"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "instructions": "snap"},
    )
    agent_id = created.json()["id"]
    await client.post(
        f"/v1/apipi/agents/{agent_id}/versions", headers=_auth(token), json={}
    )
    await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"instructions": "live"},
    )
    live = await client.get(f"/v1/apipi/agents/{agent_id}/export", headers=_auth(token))
    assert live.status_code == 200
    with zipfile.ZipFile(io.BytesIO(live.content)) as archive:
        manifest = json.loads(archive.read("agent.json"))
    assert manifest["agent"]["instructions"] == "live"
    assert "source_version" not in manifest
    pinned = await client.get(
        f"/v1/apipi/agents/{agent_id}/export",
        headers=_auth(token),
        params={"version": "1"},
    )
    with zipfile.ZipFile(io.BytesIO(pinned.content)) as archive:
        pinned_manifest = json.loads(archive.read("agent.json"))
    assert pinned_manifest["agent"]["instructions"] == "snap"
    assert pinned_manifest["source_version"]["number"] == 1
    imported = await client.post(
        "/v1/templates/import",
        headers=_auth(token),
        files={"bundle": ("agent.zip", live.content, "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    made = await client.post(
        f"/v1/templates/{imported.json()['id']}/agents",
        headers=_auth(token),
        json={},
    )
    assert made.status_code == 200, made.text
    versions = await client.get(
        f"/v1/apipi/agents/{made.json()['agent']['id']}/versions",
        headers=_auth(token),
    )
    assert versions.json()["data"] == []
