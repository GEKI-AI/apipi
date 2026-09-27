import uuid
from typing import cast

import pytest

from apipi.config import Settings
from apipi.services.runtime import (
    EventHub,
    FakeHarness,
    Harness,
    continue_turn,
    run_turn,
)
from apipi.store.engine import Store
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    list_events,
    update_session,
)


async def _session(
    store: Store, *, model: str | None = "m1", status: str = "idle"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            model=model,
            status=status,
            environment={"type": "none"},
        )
        return tenant.id, row.id


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
    tenant_id, session_id = await _session(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "model_list": "turn"}
    )
    await run_turn(
        store,
        EventHub(),
        cast(Harness, FakeHarness()),
        tenant_id,
        session_id,
        "hello",
        settings=host,
        api_key="k",
    )
    assert "agent.session.turn.completed" in await _types(store, tenant_id, session_id)


async def test_run_turn_model_required_reaches_session(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _session(store, model=None)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    await run_turn(
        store,
        EventHub(),
        cast(Harness, FakeHarness()),
        tenant_id,
        session_id,
        "hello",
        settings=host,
    )
    types = await _types(store, tenant_id, session_id)
    assert "agent.session.error" in types
    assert "agent.session.failed" in types


async def test_turn_catches_unavailable_model(store: Store, settings: Settings) -> None:
    tenant_id, session_id = await _session(store)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    harness = FakeHarness()
    harness.fail_message = "model missing is not available"
    await run_turn(
        store,
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=host,
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
    await continue_turn(
        store,
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
    )
    assert "agent.session.turn.completed" in await _types(store, tenant_id, session_id)
