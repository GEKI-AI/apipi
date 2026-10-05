import json
import uuid

import pytest
from httpx import AsyncClient
from tests.support.http import auth, session_with_turn, tenant_of
from tests.support.rows import row_json
from tests.support.split_worker import split_client_for

from apipi.config import Settings
from apipi.services.payload_export import payload_event, redact_payload
from apipi.store.engine import Store
from apipi.store.models import Item
from apipi.store.turn_logs import get_turn_log


async def test_payload_export_off_does_not_emit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[object] = []
    monkeypatch.setattr(
        "apipi.services.usage_export.HttpExporter.emit",
        lambda self, event: emitted.append(event),
    )
    token = "off-payload"
    await session_with_turn(client, token)
    assert emitted == []


async def test_payload_export_sends_items_not_turn_log(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
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
        secrets: tuple[str, ...] = (),
    ) -> None:
        event = payload_event(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            items=items,
        )
        known = tuple(
            value
            for value in (
                _settings.model_api_key_overwrite,
                _settings.payload_export_token,
            )
            if isinstance(value, str) and value
        )
        captured.append(redact_payload(event, (*known, *secrets)))

    monkeypatch.setattr("apipi.services.turn_log.export_payload", record)
    payload_settings = settings.model_copy(
        update={"payload_export_url": "http://export.test/payloads"}
    )
    token = "on-payload"
    async with split_client_for(payload_settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        session_id = await session_with_turn(client, token)
        turns = await client.get(
            f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
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
        row = await get_turn_log(db, tenant_of(token), turn_id)
    assert row is not None
    assert "hello" not in row_json(row)


async def test_payload_export_failure_does_not_break_turn(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("payload export down")

    monkeypatch.setattr("apipi.services.turn_log.export_payload", boom)
    payload_settings = settings.model_copy(
        update={"payload_export_url": "http://export.test/payloads"}
    )
    token = "payload-fail"
    async with split_client_for(payload_settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        session_id = await session_with_turn(client, token)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=auth(token)
        )
    assert session.status_code == 200
    assert session.json()["status"] == "idle"


async def test_payload_export_redacts_vault_secrets(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    worker_secret: str,
) -> None:
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "apipi.services.usage_export.HttpExporter.emit",
        lambda self, event: emitted.append(event),
    )
    payload_settings = settings.model_copy(
        update={"payload_export_url": "http://export.test/payloads"}
    )
    token = "payload-vault"
    async with split_client_for(payload_settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        vault = await client.post(
            "/v1/agents/vaults", headers=auth(token), json={"name": "v"}
        )
        vault_id = vault.json()["id"]
        cred = await client.post(
            f"/v1/agents/vaults/{vault_id}/credentials",
            headers=auth(token),
            json={
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": "https://mcp.example.com/mcp",
                    "token": "vault-secret-in-chat",
                }
            },
        )
        assert cred.status_code == 200
        agent = await client.post(
            "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "vault_ids": [vault_id],
                "input": "the key is vault-secret-in-chat",
            },
        )
        assert created.status_code == 200
    assert emitted
    dumped = json.dumps(emitted)
    assert "vault-secret-in-chat" not in dumped
    assert "[redacted]" in dumped
