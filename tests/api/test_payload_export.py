import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.payload_export import payload_event, redact_payload
from apipi.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.store.models import Item
from apipi.store.turn_logs import get_turn_log
from apipi.tokens import hash_token


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


def _row_blob(row: object) -> str:
    table = getattr(row, "__table__", None)
    if table is None:
        return str(row)
    return json.dumps(
        {column.key: getattr(row, column.key) for column in table.columns},
        default=str,
    )


async def test_payload_export_off_does_not_emit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[object] = []
    monkeypatch.setattr(
        "apipi.usage_export.HttpExporter.emit",
        lambda self, event: emitted.append(event),
    )
    token = "off-payload"
    await _session_with_turn(client, token)
    assert emitted == []


async def test_payload_export_sends_items_not_turn_log(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def record(
        _settings: Settings,
        _metrics: object,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        request_id: str | None,
        items: list[Item],
    ) -> None:
        event = payload_event(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            items=items,
        )
        secrets = tuple(
            value
            for value in (_settings.model_api_key, _settings.payload_export_token)
            if isinstance(value, str) and value
        )
        captured.append(redact_payload(event, secrets))

    monkeypatch.setattr("apipi.runtime.export_payload", record)
    payload_settings = settings.model_copy(
        update={"payload_export_url": "http://export.test/payloads"}
    )
    app = create_app(payload_settings, store=store, harness=FakeHarness())
    token = "on-payload"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _session_with_turn(client, token)
        turns = await client.get(
            f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
        )
    turn_id = uuid.UUID(turns.json()["data"][0]["id"])
    assert len(captured) == 1
    event = captured[0]
    assert event["session_id"] == session_id
    assert event["turn_id"] == str(turn_id)
    raw_items = event["items"]
    assert isinstance(raw_items, list)
    contents = [item.get("content") for item in raw_items if isinstance(item, dict)]
    assert "hello" in contents
    async with store.session() as db:
        row = await get_turn_log(db, _tenant_id(token), turn_id)
    assert row is not None
    assert "hello" not in _row_blob(row)


async def test_payload_export_failure_does_not_break_turn(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("payload export down")

    monkeypatch.setattr("apipi.runtime.export_payload", boom)
    payload_settings = settings.model_copy(
        update={"payload_export_url": "http://export.test/payloads"}
    )
    app = create_app(payload_settings, store=store, harness=FakeHarness())
    token = "payload-fail"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _session_with_turn(client, token)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
    assert session.status_code == 200
    assert session.json()["status"] == "idle"
