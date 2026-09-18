import json
import uuid
from collections.abc import AsyncIterator
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support import fake_sink

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import get_turn_log, list_turn_logs, usage_day


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant_id(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


async def _session_with_turn(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


@pytest.fixture
def off_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"usage_store": "off"})


@pytest.fixture
def rollup_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"usage_store": "rollups"})


@pytest.fixture
def export_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"usage_export_url": "http://export.test/usage"})


@pytest.fixture
async def off_client(
    off_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    app = create_app(off_settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def rollup_client(
    rollup_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    app = create_app(rollup_settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_usage_store_off_writes_no_rows(
    off_client: AsyncClient, store: Store
) -> None:
    token = "off"
    session_id = await _session_with_turn(off_client, token)
    turns = await off_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = _tenant_id(token)
    async with store.session() as db:
        assert await get_turn_log(db, tenant_id, turn_id) is None
        listed = await list_turn_logs(db, tenant_id, uuid.UUID(session_id))
        assert listed == []
        day = utc_now().date()
        assert (await usage_day(db, tenant_id, day))["turns"] == 0
    usage = await off_client.get(
        "/v1/usage", headers=_auth(token), params={"session_id": session_id}
    )
    assert usage.status_code == 200
    assert usage.json()["turns"] == 0
    assert "hello" not in str(usage.json())


async def test_usage_store_rollups_skips_turn_rows(
    rollup_client: AsyncClient, store: Store
) -> None:
    token = "rollups"
    session_id = await _session_with_turn(rollup_client, token)
    turns = await rollup_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    tenant_id = _tenant_id(token)
    async with store.session() as db:
        assert await get_turn_log(db, tenant_id, turn_id) is None
        day = utc_now().date()
        assert (await usage_day(db, tenant_id, day))["turns"] == 1
    day = utc_now().date().isoformat()
    usage = await rollup_client.get(
        "/v1/usage", headers=_auth(token), params={"day": day}
    )
    assert usage.status_code == 200
    assert usage.json()["turns"] == 1
    turn_usage = await rollup_client.get(
        "/v1/usage", headers=_auth(token), params={"turn_id": str(turn_id)}
    )
    assert turn_usage.status_code == 200
    assert turn_usage.json()["turns"] == 0


async def test_usage_export_receives_event(
    export_settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    def capture(
        settings: Settings,
        metrics: object,
        event: dict[str, object],
    ) -> None:
        del settings, metrics
        captured.append(event)

    monkeypatch.setattr("apipi.services.runtime.export_usage", capture)
    app = create_app(export_settings, store=store, harness=FakeHarness())
    token = "export"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _session_with_turn(client, token)
    assert len(captured) == 1
    event = captured[0]
    blob = json.dumps(event)
    assert "hello" not in blob
    assert event["session_id"] == session_id
    assert event["status"] == "completed"
    assert event["environment_type"] == "none"
    assert event["run_mode"] == "none"


async def test_usage_export_failure_does_not_break_turn(
    export_settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("export down")

    monkeypatch.setattr("apipi.services.runtime.export_usage", boom)
    app = create_app(export_settings, store=store, harness=FakeHarness())
    token = "export-fail"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        session = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": created.json()["id"],
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
    assert session.status_code == 200
    assert session.json()["status"] == "idle"


async def test_usage_event_includes_plugin_user_id(
    settings: Settings, store: Store
) -> None:
    fake_sink.reset()

    def auth(bearer: str) -> dict[str, str]:
        del bearer
        return {
            "key_id": "plugin-key",
            "tenant_id": str(uuid5(NAMESPACE_URL, "usage-user")),
            "user_id": "user-9",
        }

    app = create_app(
        settings.model_copy(update={"usage_sinks": "tests.support.fake_sink:FakeSink"}),
        store=store,
        harness=FakeHarness(),
        authenticate=auth,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth("t"), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("t"),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert created.headers["x-user-id"] == "plugin-key"
    assert fake_sink.events
    assert fake_sink.events[-1]["user_id"] == "user-9"
    assert fake_sink.events[-1]["key_id"] == "plugin-key"
