import asyncio
import json
import logging
import uuid

import pytest
from tests.support.logs import field

from apipi.common.background import (
    run_loop,
    sample_event_loop_lag,
    spawn_loop,
    watch_task,
)
from apipi.common.logutil import (
    ContextFilter,
    JsonFormatter,
    RateLimitedLog,
    log_context,
)
from apipi.common.metrics import Metrics
from apipi.protocol import WorkerCommand
from apipi.worker.commands import log_command

LOG = logging.getLogger("apipi.test.worker")


class _Clock:
    def __init__(self) -> None:
        self.now = 30.0

    def __call__(self) -> float:
        return self.now


def test_rate_limited_log_first_then_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOG.name)
    clock = _Clock()
    limited = RateLimitedLog(LOG, interval=60.0, clock=clock)
    assert limited.warning("late", event="worker.heartbeat.late", error_code="x")
    for _ in range(5):
        clock.now += 1
        assert not limited.warning("late", event="worker.heartbeat.late")
    assert limited.warning("other", event="worker.other")
    clock.now += 60
    assert limited.warning("late", event="worker.heartbeat.late", error_code="x")
    lines = [r for r in caplog.records if field(r, "event") == "worker.heartbeat.late"]
    assert [field(line, "count") for line in lines] == [1, 6]
    assert field(lines[1], "error_code") == "x"
    assert len([r for r in caplog.records if field(r, "event") == "worker.other"]) == 1


def test_rate_limited_log_keys_are_independent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOG.name)
    clock = _Clock()
    limited = RateLimitedLog(LOG, clock=clock)
    assert limited.warning("a", event="e", key="one")
    assert limited.warning("a", event="e", key="two")
    assert not limited.warning("a", event="e", key="one")


async def test_log_context_reaches_tasks_and_formatter() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    with log_context(worker_id="w1", connection_id="c1"):

        async def child() -> logging.LogRecord:
            made = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
            made.worker_id = "own"
            ContextFilter().filter(made)
            return made

        made = await asyncio.create_task(child())
        ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["worker_id"] == "w1"
    assert payload["connection_id"] == "c1"
    assert field(made, "worker_id") == "own"
    assert field(made, "connection_id") == "c1"
    outside = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    ContextFilter().filter(outside)
    assert not hasattr(outside, "connection_id")


async def test_run_loop_survives_errors_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi.background")
    metrics = Metrics()
    calls = 0

    async def body() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")

    async def fast(_seconds: float) -> None:
        await asyncio.sleep(0)

    await run_loop(
        "lease_reaper", body, interval=1, metrics=metrics, sleep=fast, rounds=4
    )
    assert calls == 4
    text = metrics.scrape().decode()
    assert 'apipi_background_loop_errors_total{loop="lease_reaper"} 2.0' in text
    assert 'apipi_background_loop_last_run_timestamp{loop="lease_reaper"}' in text
    errors = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "background.loop.error"
    ]
    assert len(errors) == 1
    assert field(errors[0], "error_code") == "background_loop_error"
    assert field(errors[0], "loop") == "lease_reaper"


@pytest.mark.parametrize("on_cancel", ["raise", "error", "swallow"])
async def test_run_loop_ends_on_cancel_whatever_the_round_does(on_cancel: str) -> None:
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if on_cancel == "raise":
                raise
            if on_cancel == "error":
                raise ValueError("Connection closed") from None

    task = spawn_loop("t", body, interval=0)
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=5)
    assert task in done
    assert task.cancelled()


async def test_watch_task_logs_a_dead_task(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="apipi.background")
    metrics = Metrics()

    async def dies() -> None:
        raise ValueError("gone")

    task = asyncio.create_task(dies())
    watch_task(task, "dies", metrics=metrics)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert any(
        getattr(r, "event", None) == "background.task.failed" for r in caplog.records
    )
    assert 'apipi_background_loop_errors_total{loop="dies"} 1.0' in (
        metrics.scrape().decode()
    )


async def test_event_loop_lag_sampler_records_lateness() -> None:
    metrics = Metrics()
    clock_values = iter([0.0, 1.25, 1.25, 2.25])

    async def instant(_seconds: float) -> None:
        return None

    await sample_event_loop_lag(
        metrics, sleep=instant, clock=lambda: next(clock_values), rounds=2
    )
    text = metrics.scrape().decode()
    assert "apipi_event_loop_lag_seconds_count 2.0" in text
    assert "apipi_event_loop_lag_seconds_sum 0.25" in text


async def test_event_loop_lag_sampler_sees_a_blocked_loop() -> None:
    import time

    metrics = Metrics()
    task = asyncio.create_task(sample_event_loop_lag(metrics, interval=0.02, rounds=2))
    await asyncio.sleep(0)
    time.sleep(0.15)
    await task
    text = metrics.scrape().decode()
    sum_line = next(
        line
        for line in text.splitlines()
        if line.startswith("apipi_event_loop_lag_seconds_sum")
    )
    assert float(sum_line.split()[-1]) > 0.1


def test_command_log_never_carries_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.worker")
    secrets = [
        "sk-model-secret",
        "mcp-key-secret",
        "vault-header-secret",
        "https://bucket.example/presigned?X-Amz-Signature=abc",
        "the user prompt text",
    ]
    command = WorkerCommand.model_validate(
        {
            "type": "command",
            "id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "lease_id": str(uuid.uuid4()),
            "op": "turn.start",
            "payload": {
                "tenant_id": str(uuid.uuid4()),
                "request_id": "req-1",
                "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
                "context": {
                    "model": {"api_key": secrets[0], "name": "m"},
                    "mcp_servers": [
                        {
                            "label": "x",
                            "url": "https://mcp.example",
                            "headers": {"Authorization": secrets[1]},
                            "vault_headers": {"X-Vault": secrets[2]},
                        }
                    ],
                    "files": [{"url": secrets[3]}],
                    "input": secrets[4],
                },
            },
        }
    )
    log_command(command)
    record = next(r for r in caplog.records if field(r, "event") == "worker.command")
    rendered = JsonFormatter().format(record)
    for secret in secrets:
        assert secret not in rendered
    payload = json.loads(rendered)
    assert payload["op"] == "turn.start"
    assert payload["request_id"] == "req-1"
    assert payload["command_id"] == str(command.command_id)
    assert payload["traceparent"].startswith("00-")
