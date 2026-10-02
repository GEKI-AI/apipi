import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from apipi.config import Settings
from apipi.services.runtime import EventHub
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.repo import create_session, create_tenant
from apipi.worker.hub import WorkerConnection, WorkerHub, _run_command
from apipi.worker.placement import placement_for, worker_accepts


def test_env_none_places_none() -> None:
    assert placement_for(environment={"type": "none"}) == "none"


@pytest.mark.parametrize("env_type", ["openai_hosted", "hosted"])
def test_computer_is_microvm(env_type: str) -> None:
    assert placement_for(environment={"type": env_type}) == "microvm"


def test_missing_environment_is_microvm() -> None:
    assert placement_for(environment=None) == "microvm"
    assert placement_for(environment={}) == "microvm"


def test_session_kind_is_ignored() -> None:
    assert (
        placement_for(
            environment={"type": "openai_hosted"},
        )
        == "microvm"
    )
    assert (
        placement_for(
            environment={"type": "none"},
        )
        == "none"
    )


def test_worker_accepts_set_membership() -> None:
    assert worker_accepts({"none", "microvm"}, "none")
    assert worker_accepts({"none", "microvm"}, "microvm")
    assert worker_accepts({"microvm"}, "microvm")
    assert not worker_accepts({"microvm"}, "none")
    assert worker_accepts({"none"}, "none")
    assert not worker_accepts({"none"}, "microvm")
    assert not worker_accepts(set(), "none")
    assert not worker_accepts(None, "none")


async def test_session_stop_kills_the_guest() -> None:
    execution = MagicMock()
    execution.teardown = AsyncMock()
    execution.store = None
    execution.settings = None
    await _run_command(
        execution,
        "session.stop",
        uuid.uuid4(),
        uuid.uuid4(),
        {"tenant_id": str(uuid.uuid4())},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.teardown.assert_awaited()


def _execution(*, run_mode: str, accepts: list[str] | None = None) -> MagicMock:
    from apipi.worker.accepts import resolved_worker_accepts

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        **({"worker_accepts": accepts} if accepts is not None else {}),
    )
    assert resolved_worker_accepts(settings)
    execution = MagicMock()
    execution.settings = settings
    execution.store = None
    execution.hub = None
    execution.run_turn = AsyncMock()
    return execution


async def test_turn_start_rejects_microvm_on_none_only() -> None:
    execution = _execution(run_mode="none")
    await _run_command(
        execution,
        "turn.start",
        uuid.uuid4(),
        uuid.uuid4(),
        {"text": "hi", "run_mode": "microvm"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_not_called()


async def test_turn_start_rejects_none_on_microvm_only() -> None:
    execution = _execution(run_mode="microvm", accepts=["microvm"])
    await _run_command(
        execution,
        "turn.start",
        uuid.uuid4(),
        uuid.uuid4(),
        {"text": "hi", "run_mode": "none"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_not_called()


async def test_turn_start_both_accepts_both() -> None:
    for required in ("none", "microvm"):
        execution = _execution(run_mode="microvm")
        await _run_command(
            execution,
            "turn.start",
            uuid.uuid4(),
            uuid.uuid4(),
            {"text": "hi", "run_mode": required},
            request_id=None,
            api_key=None,
            key_id=None,
            user_id=None,
        )
        execution.run_turn.assert_awaited_once()


async def test_mismatched_turn_persists_error(store: Store) -> None:
    hub = EventHub()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )
        tenant_id = tenant.id
        session_id = row.id
    execution = MagicMock()
    execution.settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        worker_accepts=["microvm"],
    )
    execution.store = store
    execution.hub = hub
    execution.run_turn = AsyncMock()
    await _run_command(
        execution,
        "turn.start",
        tenant_id,
        session_id,
        {"text": "hi", "run_mode": "none"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_not_called()
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert "agent.session.error" in types
    assert "agent.session.turn.failed" in types
    error = next(event for event in events if event.type == "agent.session.error")
    assert error.data["code"] == "placement"


def test_pick_filters_accepts() -> None:
    hub = WorkerHub(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            microvm_mem_mib=512,
        )
    )
    none_only = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=8,
        memory_mb=4096,
        run_mode="none",
        accepts=frozenset({"none"}),
    )
    microvm_only = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=8,
        memory_mb=8192,
        run_mode="microvm",
        accepts=frozenset({"microvm"}),
    )
    hub._conns[none_only.worker_id] = none_only
    hub._conns[microvm_only.worker_id] = microvm_only
    assert hub.pick(kind="none") is none_only
    assert hub.pick(kind="microvm") is microvm_only


def test_pick_both_worker_gets_both_kinds() -> None:
    hub = WorkerHub(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="microvm",
            microvm_mem_mib=512,
        )
    )
    both = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=8,
        memory_mb=8192,
        run_mode="microvm",
        accepts=frozenset({"none", "microvm"}),
    )
    hub._conns[both.worker_id] = both
    assert hub.pick(kind="none") is both
    assert hub.pick(kind="microvm") is both
