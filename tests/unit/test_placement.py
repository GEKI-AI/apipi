import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from apipi.common.event_bus import EventHub
from apipi.common.placement import placement_for, worker_accepts
from apipi.config import Settings
from apipi.worker.commands import _run_command
from apipi.worker.outbox import Outbox
from apipi.worker.sink import OutboxSink


@pytest.mark.parametrize(
    ("environment", "want"),
    [
        ({"type": "none"}, "none"),
        ({"type": "openai_hosted"}, "microvm"),
        ({"type": "hosted"}, "microvm"),
        ({}, "microvm"),
        (None, "microvm"),
    ],
)
def test_only_environment_type_none_places_none(
    environment: dict[str, str] | None, want: str
) -> None:
    assert placement_for(environment=environment) == want


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


async def test_mismatched_turn_reports_error() -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    outbox = Outbox()
    execution = MagicMock()
    execution.settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        worker_accepts=["microvm"],
    )
    execution.hub = EventHub()
    execution.run_turn = AsyncMock()
    execution.sink_for = MagicMock(
        return_value=OutboxSink(outbox, tenant_id, session_id)
    )
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
    events = [
        item["payload"]
        for item in outbox.pending(session_id)
        if item["type"] == "event"
    ]
    types = [event["type"] for event in events]
    assert "agent.session.error" in types
    assert "agent.session.turn.failed" in types
    error = next(event for event in events if event["type"] == "agent.session.error")
    assert error["data"]["code"] == "placement"
