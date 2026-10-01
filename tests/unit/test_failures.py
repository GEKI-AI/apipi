import logging
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from apipi.config import Settings
from apipi.services.failures import (
    FIXTURE_PI,
    Failure,
    classify_host_message,
    failure_for,
    log_level_for,
    log_level_for_code,
    pi_payload,
)
from apipi.services.runtime import (
    EventHub,
    FakeHarness,
    Harness,
    _cancel_turn,
    _fail_turn,
    fail_stale_in_progress,
    run_turn,
)
from apipi.store.engine import Store
from apipi.store.repo import (
    create_session,
    create_tenant,
    create_turn,
    get_turn_log,
    list_events,
)
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.version import PINNED_PI


def test_fixtures_match_pinned_pi() -> None:
    assert FIXTURE_PI == PINNED_PI == "0.99.1"


@pytest.mark.parametrize(
    ("text", "code", "status", "retryable"),
    [
        ("429 Rate limit reached for requests", "upstream_rate_limited", 429, True),
        ("500 Internal Server Error", "upstream_5xx", 500, True),
        ("502 Bad Gateway", "upstream_5xx", 502, True),
        ('503: {"error":"unavailable"}', "upstream_5xx", 503, True),
        ("524: <html>cloudflare</html>", "upstream_5xx", 524, True),
        ("Request timed out.", "upstream_timeout", None, True),
        ("408 Request Timeout", "upstream_timeout", 408, True),
        ("504 status code (no body)", "upstream_timeout", 504, True),
        ('504: {"error":{"message":"timeout"}}', "upstream_timeout", 504, True),
        ("Connection error.", "upstream_connection", None, True),
        (
            "Provider finish_reason: network_error",
            "upstream_connection",
            None,
            True,
        ),
        (
            "401 Incorrect API key provided: sk-secret",
            "upstream_unauthorized",
            401,
            False,
        ),
        (
            'OpenAI API error (401): {"message":"Incorrect API key provided: secret"}',
            "upstream_unauthorized",
            401,
            False,
        ),
        ("403 status code (no body)", "upstream_unauthorized", 403, False),
        (
            "Your input exceeds the context window of this model",
            "context_length_exceeded",
            None,
            False,
        ),
        (
            '400: {"error":{"code":"context_length_exceeded"}}',
            "context_length_exceeded",
            400,
            False,
        ),
        (
            "Provider finish_reason: content_filter",
            "upstream_content_filter",
            None,
            False,
        ),
        ("400 Invalid parameter", "upstream_4xx", 400, False),
        ("No model configured for provider", "upstream_error", None, False),
    ],
)
def test_pi_0851_error_message(
    text: str, code: str, status: int | None, retryable: bool
) -> None:
    failure = classify_host_message(text)
    assert failure.code == code
    assert failure.failure_source == "upstream"
    assert failure.upstream_status == status
    assert failure.retryable is retryable
    assert failure.legacy_code == "model_host_error"
    assert "sk-secret" not in failure.message
    assert "secret" not in failure.message or "Incorrect" not in text


def test_unauthorized_masks_both_status_forms() -> None:
    leading = classify_host_message("401 Incorrect API key provided: sk-secret")
    paren = classify_host_message(
        'OpenAI API error (403): {"message":"Incorrect API key provided: secret"}'
    )
    assert leading.message == "Model host error (401)"
    assert paren.message == "Model host error (403)"
    assert "sk-secret" not in leading.message
    assert "secret" not in paren.message


@pytest.mark.parametrize(
    ("code", "source", "level"),
    [
        ("cancelled", "user", logging.INFO),
        ("client_disconnected", "user", logging.INFO),
        ("invalid_request", "user", logging.WARNING),
        ("model_required", "user", logging.WARNING),
        ("model_not_found", "user", logging.WARNING),
        ("upstream_rate_limited", "upstream", logging.WARNING),
        ("upstream_unauthorized", "upstream", logging.WARNING),
        ("context_length_exceeded", "upstream", logging.WARNING),
        ("upstream_content_filter", "upstream", logging.WARNING),
        ("upstream_4xx", "upstream", logging.WARNING),
        ("upstream_5xx", "upstream", logging.ERROR),
        ("upstream_timeout", "upstream", logging.ERROR),
        ("upstream_connection", "upstream", logging.ERROR),
        ("upstream_error", "upstream", logging.ERROR),
        ("turn_timeout", "internal", logging.ERROR),
        ("pi_exited", "internal", logging.ERROR),
        ("pi_memory", "internal", logging.ERROR),
        ("spawn_failed", "internal", logging.ERROR),
        ("artifact_store", "internal", logging.ERROR),
        ("turn_interrupted", "internal", logging.ERROR),
        ("worker_lease_expired", "internal", logging.ERROR),
        ("internal", "internal", logging.ERROR),
    ],
)
def test_log_level_table(code: str, source: str, level: int) -> None:
    failure = Failure(
        message="x",
        code=code,
        failure_source=source,
        retryable=False,
    )
    assert log_level_for(failure) == level
    assert log_level_for_code(code, source) == level


async def _ready(store: Store) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, model="m1", status="idle", environment={"type": "none"}
        )
        return tenant.id, row.id


async def test_upstream_turn_keeps_legacy_public_code(
    store: Store, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "error_codes": "legacy"}
    )
    harness = FakeHarness()
    harness.fail_message = "429 Rate limit reached for requests"
    caplog.set_level(logging.WARNING, logger="apipi")
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
        turns = [event for event in events if event.type == "agent.session.turn.failed"]
        error = next(event for event in events if event.type == "agent.session.error")
        turn_id = uuid.UUID(str(turns[0].data["turn_id"]))
        log_row = await get_turn_log(db, tenant_id, turn_id)
    assert isinstance(turns[0].data, dict)
    assert turns[0].data["code"] == "upstream_rate_limited"
    assert turns[0].data["failure_source"] == "upstream"
    assert turns[0].data["upstream_status"] == 429
    assert turns[0].data["retryable"] is True
    assert turns[0].data["legacy_code"] == "model_host_error"
    assert turns[0].data["upstream_attempts"] == 1
    assert isinstance(error.data, dict)
    assert error.data["code"] == "model_host_error"
    assert error.data["detail_code"] == "upstream_rate_limited"
    assert log_row is not None
    assert log_row.error_code == "upstream_rate_limited"
    assert log_row.failure_source == "upstream"
    assert log_row.legacy_code == "model_host_error"
    failed = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "turn.failed"
    ]
    assert failed[-1].levelno == logging.WARNING


async def test_specific_mode_puts_code_on_session_error(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(
        update={
            "model_base_url": "http://model.test/v1",
            "error_codes": "specific",
        }
    )
    harness = FakeHarness()
    harness.fail_message = "503: overloaded"
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
    assert error.data["code"] == "upstream_5xx"
    assert error.data["detail_code"] == "upstream_5xx"
    assert error.data["legacy_code"] == "model_host_error"


def test_error_codes_default_is_specific() -> None:
    assert Settings.model_fields["error_codes"].default == "specific"


async def test_default_error_codes_are_specific(
    store: Store, settings: Settings
) -> None:
    assert settings.error_codes == "specific"
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    harness = FakeHarness()
    harness.fail_message = "503: overloaded"
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
    assert error.data["code"] == "upstream_5xx"
    assert error.data["legacy_code"] == "model_host_error"


async def test_cancel_event_is_user(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi")
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, model="m1", status="in_progress")
        turn = await create_turn(db, tenant.id, row.id, status="in_progress")
        await _cancel_turn(db, EventHub(), tenant.id, row.id, turn.id)
        events = await list_events(db, tenant.id, row.id)
    cancelled = next(
        event for event in events if event.type == "agent.session.turn.cancelled"
    )
    assert isinstance(cancelled.data, dict)
    assert cancelled.data["failure_source"] == "user"
    assert cancelled.data["code"] == "cancelled"
    assert cancelled.data["reason"] == "user"
    info = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "turn" and record.levelno == logging.INFO
    ]
    assert info
    assert info[-1].__dict__["error_code"] == "cancelled"


async def test_timeout_is_not_a_cancel(store: Store, settings: Settings) -> None:
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    harness = FakeHarness()
    harness.hold = True
    await run_turn(
        store,
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        "hello",
        settings=host,
        turn_timeout=timedelta(milliseconds=20),
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    types = [event.type for event in events]
    assert "agent.session.turn.cancelled" not in types
    failed = next(
        event for event in events if event.type == "agent.session.turn.failed"
    )
    assert isinstance(failed.data, dict)
    assert failed.data["code"] == "turn_timeout"
    assert failed.data["failure_source"] == "internal"
    assert failed.data["retryable"] is True
    assert failed.data["upstream_attempts"] == 1


class _Dead:
    def __init__(self, reason: str | None) -> None:
        self.stop_reason = reason

    async def prompt(self, text: str) -> Any:
        del text
        if False:
            yield {}


class _Pool:
    def __init__(self, reason: str | None) -> None:
        self.proc = _Dead(reason)

    async def get(self, *_args: object, **_kwargs: object) -> _Dead:
        return self.proc

    def touch(self, _session_id: uuid.UUID) -> None:
        return None


async def test_unsettled_pi_is_internal() -> None:
    session_id = uuid.uuid4()
    exited = [
        event
        async for event in PiHarness(cast(Any, _Pool(None))).generate(
            "hi", session_id=session_id
        )
    ]
    memory = [
        event
        async for event in PiHarness(cast(Any, _Pool("memory"))).generate(
            "hi", session_id=session_id
        )
    ]
    assert exited[0][1]["code"] == "pi_exited"
    assert exited[0][1]["failure_source"] == "internal"
    assert exited[0][1]["retryable"] is True
    assert memory[0][1]["code"] == "pi_memory"
    assert memory[0][1]["retryable"] is False


async def test_internal_fail_turn_logs_error(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi")
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, model="m1", status="in_progress")
        first = await create_turn(db, tenant.id, row.id, status="in_progress")
        second = await create_turn(db, tenant.id, row.id, status="in_progress")
        await _fail_turn(
            db,
            EventHub(),
            tenant.id,
            row.id,
            first.id,
            "Cannot start Pi",
            code="spawn_failed",
        )
        await _fail_turn(
            db,
            EventHub(),
            tenant.id,
            row.id,
            second.id,
            "store down",
            code="artifact_store",
        )
        events = await list_events(db, tenant.id, row.id)
    codes = [
        event.data.get("code")
        for event in events
        if event.type == "agent.session.turn.failed" and isinstance(event.data, dict)
    ]
    assert codes == ["spawn_failed", "artifact_store"]
    assert all(
        event.data.get("failure_source") == "internal"
        for event in events
        if event.type == "agent.session.turn.failed" and isinstance(event.data, dict)
    )
    logged = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "turn.failed"
    ]
    assert logged
    assert all(record.levelno == logging.ERROR for record in logged)


class _Os:
    async def abort(self, _session_id: uuid.UUID) -> None:
        return None

    async def generate(self, *_args: object, **_kwargs: object) -> Any:
        raise OSError("missing pi")
        yield ("usage", {})


async def test_spawn_oserror_is_spawn_failed(store: Store, settings: Settings) -> None:
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(update={"model_base_url": "http://model.test/v1"})
    await run_turn(
        store,
        EventHub(),
        cast(Harness, _Os()),
        tenant_id,
        session_id,
        "hello",
        settings=host,
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    failed = next(
        event for event in events if event.type == "agent.session.turn.failed"
    )
    assert isinstance(failed.data, dict)
    assert failed.data["code"] == "spawn_failed"
    assert failed.data["failure_source"] == "internal"


class _Exited:
    async def abort(self, _session_id: uuid.UUID) -> None:
        return None

    async def generate(self, *_args: object, **_kwargs: object) -> Any:
        yield (
            "pi_error",
            pi_payload(failure_for("pi_exited", "Pi stopped before the turn finished")),
        )


async def test_pi_exited_keeps_legacy_error_code(
    store: Store, settings: Settings
) -> None:
    tenant_id, session_id = await _ready(store)
    host = settings.model_copy(
        update={"model_base_url": "http://model.test/v1", "error_codes": "legacy"}
    )
    await run_turn(
        store,
        EventHub(),
        cast(Harness, _Exited()),
        tenant_id,
        session_id,
        "hello",
        settings=host,
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    failed = next(
        event for event in events if event.type == "agent.session.turn.failed"
    )
    error = next(event for event in events if event.type == "agent.session.error")
    assert isinstance(failed.data, dict)
    assert isinstance(error.data, dict)
    assert failed.data["code"] == "pi_exited"
    assert failed.data["failure_source"] == "internal"
    assert error.data["code"] == "model_host_error"
    assert error.data["detail_code"] == "pi_exited"


async def test_stale_turn_is_interrupted(store: Store) -> None:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, model="m1", status="in_progress")
        await create_turn(db, tenant.id, row.id, status="in_progress")
        await fail_stale_in_progress(db, EventHub(), tenant.id, row.id)
        events = await list_events(db, tenant.id, row.id)
    failed = next(
        event for event in events if event.type == "agent.session.turn.failed"
    )
    assert isinstance(failed.data, dict)
    assert failed.data["code"] == "turn_interrupted"
    assert failed.data["failure_source"] == "internal"
    assert failed.data["retryable"] is True
