import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from tests.support.worker_turn import new_session, run_worker_turn

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.repo import get_turn_log, list_events
from apipi.worker.pi.map import map_pi_event


class Scripted:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self.events = events
        self.aborted = False

    async def abort(self, session_id: uuid.UUID) -> None:
        del session_id
        self.aborted = True

    async def generate(
        self,
        text: str,
        *,
        abort: asyncio.Event | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        del text
        for item in self.events:
            if abort is not None and abort.is_set():
                return
            yield item


def _error_end(message: str, *, will_retry: bool) -> dict[str, Any]:
    return {
        "type": "agent_end",
        "willRetry": will_retry,
        "messages": [
            {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": message,
            }
        ],
    }


def _mapped(*events: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        out.extend(map_pi_event(event))
    return out


async def test_recorded_retry_then_success_completes(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    events = _mapped(
        _error_end("504: timeout", will_retry=True),
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 2000,
            "errorMessage": "504: timeout",
        },
        {
            "type": "auto_retry_end",
            "success": True,
            "attempt": 1,
        },
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "stopReason": "stop"}],
        },
    )
    events.append(("agent.session.turn.output_text.done", {"text": "ok"}))
    await run_worker_turn(
        store,
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        Scripted(events),
        tenant_id,
        session_id,
    )
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
    types = [event.type for event in stored]
    assert "agent.session.turn.failed" not in types
    assert "agent.session.turn.retrying" in types
    assert "agent.session.turn.retry.completed" in types
    assert "agent.session.turn.completed" in types
    retrying = next(
        event for event in stored if event.type == "agent.session.turn.retrying"
    )
    assert isinstance(retrying.data, dict)
    assert retrying.data["code"] == "upstream_timeout"
    assert retrying.data["upstream_status"] == 504
    assert retrying.data["attempt"] == 1


async def test_final_attempt_is_classified(store: Store, settings: Settings) -> None:
    tenant_id, session_id = await new_session(store)
    events = _mapped(
        _error_end("429 Rate limit reached", will_retry=True),
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 1,
            "delayMs": 2000,
            "errorMessage": "429 Rate limit reached",
        },
        _error_end("400 Invalid parameter", will_retry=False),
    )
    await run_worker_turn(
        store,
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        Scripted(events),
        tenant_id,
        session_id,
    )
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
        failed = next(
            event for event in stored if event.type == "agent.session.turn.failed"
        )
        error = next(event for event in stored if event.type == "agent.session.error")
        assert isinstance(failed.data, dict)
        turn_id = uuid.UUID(str(failed.data["turn_id"]))
        log_row = await get_turn_log(db, tenant_id, turn_id)
    assert failed.data["code"] == "upstream_4xx"
    assert failed.data["upstream_attempts"] == 2
    assert isinstance(error.data, dict)
    assert error.data["upstream_attempts"] == 2
    assert log_row is not None
    assert log_row.upstream_attempts == 2
    assert log_row.error_code == "upstream_4xx"


async def test_plain_400_fails_after_one_attempt(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    events = _mapped(_error_end("400 Invalid parameter", will_retry=False))
    await run_worker_turn(
        store,
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        Scripted(events),
        tenant_id,
        session_id,
    )
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
    failed = next(
        event for event in stored if event.type == "agent.session.turn.failed"
    )
    assert isinstance(failed.data, dict)
    assert failed.data["code"] == "upstream_4xx"
    assert failed.data["upstream_attempts"] == 1
    assert "agent.session.turn.retrying" not in [event.type for event in stored]


class _CancelInBackoff:
    async def abort(self, session_id: uuid.UUID) -> None:
        del session_id

    async def generate(
        self,
        text: str,
        *,
        abort: asyncio.Event | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        del text
        for item in _mapped(
            _error_end("504: timeout", will_retry=True),
            {
                "type": "auto_retry_start",
                "attempt": 1,
                "maxAttempts": 3,
                "delayMs": 2000,
                "errorMessage": "504: timeout",
            },
            {
                "type": "auto_retry_end",
                "success": False,
                "attempt": 1,
                "finalError": "Retry cancelled",
            },
        ):
            yield item
        if abort is not None:
            abort.set()


async def test_cancel_during_backoff_is_not_upstream(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    await run_worker_turn(
        store,
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        _CancelInBackoff(),
        tenant_id,
        session_id,
    )
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
    types = [event.type for event in stored]
    assert "agent.session.turn.failed" not in types
    assert "agent.session.turn.cancelled" in types


class _HangAfterRetry:
    async def abort(self, session_id: uuid.UUID) -> None:
        del session_id

    async def generate(
        self,
        text: str,
        *,
        abort: asyncio.Event | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        del text, abort
        for item in _mapped(
            {
                "type": "auto_retry_start",
                "attempt": 2,
                "maxAttempts": 3,
                "delayMs": 4000,
                "errorMessage": "504: timeout",
            }
        ):
            yield item
        await asyncio.Event().wait()


async def test_turn_timeout_during_retry_records_attempts(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await new_session(store)
    await run_worker_turn(
        store,
        settings.model_copy(update={"model_base_url": "http://model.test/v1"}),
        _HangAfterRetry(),
        tenant_id,
        session_id,
        turn_timeout=timedelta(milliseconds=30),
    )
    async with store.session() as db:
        stored = await list_events(db, tenant_id, session_id)
    failed = next(
        event for event in stored if event.type == "agent.session.turn.failed"
    )
    assert isinstance(failed.data, dict)
    assert failed.data["code"] == "turn_timeout"
    assert failed.data["upstream_attempts"] == 2
