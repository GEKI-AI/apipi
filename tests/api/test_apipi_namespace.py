import logging
import uuid

from fastapi.routing import APIRoute
from httpx import AsyncClient

from apipi.gateway import create_app
from apipi.services.runtime import event_body
from apipi.services.sessions import artifact_body, item_body, session_body, turn_body
from apipi.store.models import Artifact, Event, Item, SessionRow, Turn, utc_now

_AGENTS_ALLOW = frozenset(
    {
        "/v1/agents",
        "/v1/agents/{agent_id}",
        "/v1/agents/{agent_id}/export",
        "/v1/agents/environments/{environment_id}",
        "/v1/agents/sessions",
        "/v1/agents/sessions/{session_id}",
        "/v1/agents/sessions/{session_id}/events",
        "/v1/agents/sessions/{session_id}/export",
        "/v1/agents/sessions/{session_id}/turns",
        "/v1/agents/sessions/{session_id}/turns/{turn_id}",
        "/v1/agents/sessions/{session_id}/items",
        "/v1/agents/sessions/{session_id}/artifacts",
        "/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content",
        "/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/download",
        "/v1/agents/sessions/{session_id}/artifacts/{artifact_id}",
        "/v1/agents/vaults",
        "/v1/agents/vaults/{vault_id}",
        "/v1/agents/vaults/{vault_id}/credentials",
        "/v1/agents/vaults/{vault_id}/credentials/{credential_id}",
    }
)

_CANONICAL = frozenset(
    {
        "/v1/apipi/usage",
        "/v1/apipi/templates",
        "/v1/apipi/uploads",
        "/v1/apipi/chat/sessions",
        "/v1/apipi/agents/{agent_id}/export",
        "/v1/apipi/sessions/{session_id}/export",
        "/v1/apipi/sessions/{session_id}/artifacts/{artifact_id}/download",
    }
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _paths(app: object) -> set[str]:
    found: set[str] = set()
    routes = getattr(app, "routes", [])
    for route in routes:
        router = getattr(route, "original_router", None)
        children = router.routes if router is not None else [route]
        for child in children:
            if isinstance(child, APIRoute):
                found.add(child.path)
    return found


def test_agents_namespace_has_no_new_apipi_routes(settings, store) -> None:
    app = create_app(settings, store=store)
    paths = _paths(app)
    agents = {path for path in paths if path.startswith("/v1/agents")}
    assert agents == _AGENTS_ALLOW
    assert paths >= _CANONICAL


def test_body_functions_are_the_public_shapes() -> None:
    now = utc_now()
    session = SessionRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="idle",
        environment={"type": "none", "sandbox_size": "S"},
        metadata_json={},
        required_actions=[],
        key_id="",
        vault_ids=[],
        created_at=now,
        updated_at=now,
    )
    body = session_body(session)
    assert set(body) == {
        "id",
        "agent_id",
        "agent_version",
        "status",
        "environment",
        "idle_ttl",
        "metadata",
        "required_actions",
        "user_id",
        "org_id",
        "created_at",
        "updated_at",
        "vault_ids",
        "reasoning",
    }
    assert body["environment"]["container_size"] == "small"
    event = Event(
        id=uuid.uuid4(),
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=1,
        type="agent.session.created",
        data={},
        created_at=now,
    )
    assert set(event_body(event)) == {
        "id",
        "type",
        "seq",
        "session_id",
        "created_at",
        "data",
    }
    turn = Turn(
        id=uuid.uuid4(),
        tenant_id=session.tenant_id,
        session_id=session.id,
        status="completed",
        created_at=now,
        updated_at=now,
    )
    assert set(turn_body(turn)) >= {"id", "session_id", "status"}
    item = Item(
        id=uuid.uuid4(),
        tenant_id=session.tenant_id,
        session_id=session.id,
        turn_id=turn.id,
        type="message",
        data={},
        created_at=now,
    )
    assert set(item_body(item)) == {
        "id",
        "session_id",
        "turn_id",
        "type",
        "data",
        "created_at",
    }
    artifact = Artifact(
        id=uuid.uuid4(),
        tenant_id=session.tenant_id,
        session_id=session.id,
        turn_id=turn.id,
        path="outputs/a.txt",
        content_type="text/plain",
        created_at=now,
    )
    assert set(artifact_body(artifact)) == {
        "id",
        "session_id",
        "turn_id",
        "path",
        "content_type",
        "created_at",
    }


async def test_usage_alias_and_canonical(client: AsyncClient, caplog) -> None:
    token = "namespace"
    caplog.set_level(logging.WARNING, logger="apipi.api")
    old = await client.get("/v1/usage", headers=_auth(token))
    new = await client.get("/v1/apipi/usage", headers=_auth(token))
    assert old.status_code == new.status_code == 400
    assert old.json() == new.json()
    assert any("deprecated route /v1/usage" in rec.message for rec in caplog.records)


async def test_container_size_maps_to_sandbox_size(client: AsyncClient) -> None:
    token = "container-size"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none", "container_size": "large"},
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["sandbox_size"] == "L"
    assert env["container_size"] == "large"
    clash = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {
                "type": "none",
                "container_size": "small",
                "sandbox_size": "L",
            },
        },
    )
    assert clash.status_code == 400
