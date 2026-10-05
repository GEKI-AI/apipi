"""Background loops that survive errors and show up in metrics.

`run_loop` runs one round after another. A round that raises is
counted in `apipi_background_loop_errors_total{loop}`, logged as
`background.loop.error` (rate limited), and the loop goes on. Every
finished round sets `apipi_background_loop_last_run_timestamp{loop}`, so
an alert on a stale timestamp catches a loop that stopped or hangs.
A round that raises after the task was cancelled ends the loop with
`CancelledError` instead. `watch_task` logs a task that ended with an
exception, for tasks that are not loops. `sample_event_loop_lag`
records how late the event loop wakes a one second sleep.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from apipi.common.logutil import RateLimitedLog, log_event

log = logging.getLogger("apipi.background")

LOOP_LAG_INTERVAL = 1.0

_loop_warnings = RateLimitedLog(log)


def cancelling() -> bool:
    """Whether the current task was asked to cancel.

    Cleanup that runs on a cancel, such as a database driver closing its
    connection, can raise another exception in place of the
    `CancelledError`. A loop that catches `Exception` checks this, so it
    stops instead of running on after the cancel.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def note_loop_error(metrics: Any | None, loop: str, exc: BaseException) -> None:
    """Count and log one caught error of the loop named `loop`."""
    if metrics is not None:
        metrics.observe_background_loop_error(loop)
    _loop_warnings.warning(
        "background loop error",
        event="background.loop.error",
        error_code="background_loop_error",
        key=loop,
        exc_info=exc,
        loop=loop,
        error=type(exc).__name__,
    )


def note_loop_run(metrics: Any | None, loop: str) -> None:
    if metrics is not None:
        metrics.set_background_loop_last_run(loop, time.time())


async def run_loop(
    loop: str,
    body: Callable[[], Awaitable[object]],
    *,
    interval: float,
    metrics: Any | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rounds: int | None = None,
    immediate: bool = False,
) -> None:
    """Run `body` every `interval` seconds until cancelled.

    The sleep comes first unless `immediate` is set, which runs the first
    round at once. `rounds` stops the loop after that many rounds, for
    tests.
    """
    done = 0
    while rounds is None or done < rounds:
        if not (immediate and done == 0):
            await sleep(interval)
        try:
            await body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if cancelling():
                raise asyncio.CancelledError from exc
            note_loop_error(metrics, loop, exc)
        note_loop_run(metrics, loop)
        done += 1


def watch_task(
    task: "asyncio.Task[Any]", name: str, *, metrics: Any | None = None
) -> None:
    """Log a task that ends with an exception. Cancellation is quiet."""

    def done(finished: "asyncio.Task[Any]") -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is None:
            return
        if metrics is not None:
            metrics.observe_background_loop_error(name)
        log_event(
            log,
            logging.ERROR,
            "background task ended with an error",
            event="background.task.failed",
            error_code="background_task_failed",
            exc_info=exc,
            loop=name,
            error=type(exc).__name__,
        )

    task.add_done_callback(done)


def spawn_loop(
    loop: str,
    body: Callable[[], Awaitable[object]],
    *,
    interval: float,
    metrics: Any | None = None,
) -> "asyncio.Task[None]":
    task = asyncio.create_task(
        run_loop(loop, body, interval=interval, metrics=metrics), name=loop
    )
    watch_task(task, loop, metrics=metrics)
    return task


async def sample_event_loop_lag(
    metrics: Any | None,
    *,
    interval: float = LOOP_LAG_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rounds: int | None = None,
) -> None:
    """Sleep `interval` seconds, again and again, and record how late each wake is."""
    done = 0
    while rounds is None or done < rounds:
        started = clock()
        await sleep(interval)
        lag = max(clock() - started - interval, 0.0)
        if metrics is not None:
            metrics.observe_event_loop_lag(lag)
        done += 1


def start_event_loop_lag(metrics: Any | None) -> "asyncio.Task[None]":
    task = asyncio.create_task(sample_event_loop_lag(metrics), name="event_loop_lag")
    watch_task(task, "event_loop_lag", metrics=metrics)
    return task
