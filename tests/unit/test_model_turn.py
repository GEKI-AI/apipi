import uuid
from typing import cast

import pytest
from tests.support.worker_turn import ingest_outbox, new_session, run_worker_turn

from apipi.config import Settings
from apipi.services.runtime import EventHub, FakeHarness, Harness, continue_turn
from apipi.services.sink import OutboxSink
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    list_events,
    update_session,
)
from apipi.worker.outbox import Outbox


async def _types(
    store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[str]:
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    return [event.type for event in events]


async def test_run_turn_does_not_list_models(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("listed")

    monkeypatch.setattr("apipi.worker.pi.model_host.listed_models", boom)
    tenant_id, session_id = await new_session(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "model_list": "turn"}
    )
    await run_worker_turn(
        store,
        host,
        FakeHarness(),
        tenant_id,
        session_id,
        api_key="k",
    )
    assert "agent.session.turn.completed" in await _types(store, tenant_id, session_id)


async def test_run_turn_model_required_reaches_session(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store, model=None)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    await run_worker_turn(
        store,
        host,
        FakeHarness(),
        tenant_id,
        session_id,
    )
    types = await _types(store, tenant_id, session_id)
    assert "agent.session.error" in types
    assert "agent.session.failed" in types


async def test_turn_catches_unavailable_model(store: Store, settings: Settings) -> None:
    tenant_id, session_id = await new_session(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "error_codes": "legacy"}
    )
    harness = FakeHarness()
    harness.fail_message = "model missing is not available"
    await run_worker_turn(
        store,
        host,
        harness,
        tenant_id,
        session_id,
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    error = next(event for event in events if event.type == "agent.session.error")
    assert isinstance(error.data, dict)
    assert error.data["code"] == "model_host_error"
    assert "not available" in str(error.data["message"])
    assert "agent.session.turn.failed" in [event.type for event in events]


async def test_continue_turn_does_not_list_models(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("listed")

    monkeypatch.setattr("apipi.worker.pi.model_host.listed_models", boom)
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            model="m1",
            status="requires_action",
            environment={"type": "none"},
        )
        await update_session(
            db,
            tenant.id,
            row.id,
            changes={
                "required_actions": [{"type": "function_call", "call_id": "call-1"}]
            },
        )
        turn = await create_turn(db, tenant.id, row.id, status="in_progress")
        tenant_id = tenant.id
        session_id = row.id
        turn_id = turn.id
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    context = await build_turn_context(store, host, tenant_id, session_id)
    outbox = Outbox()
    await continue_turn(
        EventHub(),
        cast(Harness, FakeHarness()),
        tenant_id,
        session_id,
        turn_id=turn_id,
        call_id="call-1",
        success=True,
        output="ok",
        error=None,
        settings=host,
        api_key="k",
        turn_context=context,
        sink=OutboxSink(outbox, tenant_id, session_id),
    )
    await ingest_outbox(store, host, outbox, tenant_id, session_id)
    assert "agent.session.turn.completed" in await _types(store, tenant_id, session_id)
