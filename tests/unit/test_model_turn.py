import uuid
from typing import cast

import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
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


def _unreachable() -> ApiError:
    return ApiError(
        "invalid_request",
        "Model host /models is unreachable",
        code="model_host_unreachable",
        status_code=400,
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


async def _codes(
    store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[str]:
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    codes: list[str] = []
    for event in events:
        if event.type != "agent.session.error":
            continue
        data = event.data if isinstance(event.data, dict) else {}
        code = data.get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


async def test_run_turn_404_reaches_session(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise _unreachable()

    monkeypatch.setattr("apipi.services.runtime.models_for_turn", boom)
    tenant_id, session_id = await _session(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "model_list": "turn"}
    )
    with pytest.raises(ApiError) as exc:
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
    assert exc.value.code == "model_host_unreachable"
    assert "model_host_unreachable" in await _codes(store, tenant_id, session_id)


async def test_run_turn_model_required_reaches_session(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _session(store, model=None)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    with pytest.raises(ApiError) as exc:
        await run_turn(
            store,
            EventHub(),
            cast(Harness, FakeHarness()),
            tenant_id,
            session_id,
            "hello",
            settings=host,
        )
    assert exc.value.code == "model_required"
    assert "model_required" in await _codes(store, tenant_id, session_id)


async def test_run_turn_unknown_model_reaches_session(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def other(*_args: object, **_kwargs: object) -> list[str]:
        return ["other"]

    monkeypatch.setattr("apipi.services.runtime.models_for_turn", other)
    tenant_id, session_id = await _session(store)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    with pytest.raises(ApiError) as exc:
        await run_turn(
            store,
            EventHub(),
            cast(Harness, FakeHarness()),
            tenant_id,
            session_id,
            "hello",
            settings=host,
        )
    assert exc.value.code == "model_not_found"
    assert "model_not_found" in await _codes(store, tenant_id, session_id)


async def test_continue_turn_404_reaches_session(
    store: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise _unreachable()

    monkeypatch.setattr("apipi.services.runtime.models_for_turn", boom)
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
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "model_list": "turn"}
    )
    with pytest.raises(ApiError) as exc:
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
    assert exc.value.code == "model_host_unreachable"
    assert "model_host_unreachable" in await _codes(store, tenant_id, session_id)
