import asyncio
import inspect
from collections.abc import Callable
from typing import Any


async def until(
    predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.01
) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if loop.time() >= deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


async def closed_on_cancel(entered: asyncio.Event) -> None:
    entered.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise ValueError("Connection closed") from None
