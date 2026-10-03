import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.websockets import WebSocketState

from apipi.workerhub.wire import send_frame

if TYPE_CHECKING:
    from apipi.workerhub.connection import WorkerConnection

log = logging.getLogger("apipi.worker")

WRITE_TIMEOUT = 10.0
SEND_QUEUE_LIMIT = 1024


class WorkerSendError(Exception):
    """A frame could not be written to the worker socket."""


class ConnectionWriter:
    """The only place that writes to one worker socket.

    Every frame, whoever sends it (the receive path, an HTTP request,
    the lease reaper), goes through one bounded queue and one task, so
    frames never interleave and a slow worker never holds more than the
    queue. A frame that is not written within `timeout` seconds, or a
    queue that is full, closes the connection with the reason
    `write_timeout`. A writer that was not started writes inline under a
    lock, with the same timeout.
    """

    def __init__(
        self,
        conn: "WorkerConnection",
        *,
        timeout: float = WRITE_TIMEOUT,
        limit: int = SEND_QUEUE_LIMIT,
    ) -> None:
        self._conn = conn
        self.timeout = timeout
        self.metrics: Any | None = None
        self.on_depth: Callable[[], None] | None = None
        self._queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[None] | None]]
        self._queue = asyncio.Queue(maxsize=limit)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._failure: BaseException | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def failed(self) -> bool:
        return self._failure is not None

    def bind(self, metrics: Any | None, on_depth: Callable[[], None] | None) -> None:
        self.metrics = metrics
        self.on_depth = on_depth

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="worker_writer")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._fail_pending(self._failure or WorkerSendError("connection closed"))

    async def send(self, payload: dict[str, Any]) -> None:
        """Queue one frame and wait until it was written."""
        if self._task is None:
            await self._send_inline(payload)
            return
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._enqueue(payload, done)
        await done

    def submit(self, payload: dict[str, Any]) -> "asyncio.Future[None]":
        """Queue one frame at once; the future is done when it was written."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._enqueue(payload, done)
        return done

    def send_nowait(self, payload: dict[str, Any]) -> None:
        """Queue one frame without waiting. A failure closes the connection."""
        if self._task is None:
            task = asyncio.create_task(self._send_inline(payload))
            task.add_done_callback(_quiet)
            return
        try:
            self._enqueue(payload, None)
        except WorkerSendError:
            return

    def _enqueue(
        self, payload: dict[str, Any], done: "asyncio.Future[None] | None"
    ) -> None:
        if self._failure is not None:
            raise WorkerSendError("connection closed") from self._failure
        try:
            self._queue.put_nowait((payload, done))
        except asyncio.QueueFull:
            self._timed_out("queue full")
            raise WorkerSendError("send queue full") from None
        self._observe()

    async def _send_inline(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            try:
                await asyncio.wait_for(self._write(payload), timeout=self.timeout)
            except TimeoutError:
                self._timed_out("write timeout")
                raise WorkerSendError("write timeout") from None

    async def _write(self, payload: dict[str, Any]) -> None:
        websocket = self._conn.websocket
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        await send_frame(websocket, payload, metrics=self.metrics)

    async def _run(self) -> None:
        while True:
            payload, done = await self._queue.get()
            self._observe()
            try:
                await asyncio.wait_for(self._write(payload), timeout=self.timeout)
            except TimeoutError:
                self._timed_out("write timeout")
                error: BaseException = WorkerSendError("write timeout")
                self._failure = error
                self._settle(done, error)
                self._fail_pending(error)
                return
            except asyncio.CancelledError:
                self._settle(done, WorkerSendError("connection closed"))
                raise
            except Exception as exc:
                self._failure = exc
                self._settle(done, exc)
                self._fail_pending(exc)
                return
            self._settle(done, None)

    def _timed_out(self, why: str) -> None:
        self._conn.warnings.warning(
            "worker write timed out; closing the connection",
            event="worker.write.timeout",
            error_code="write_timeout",
            worker_id=self._conn.worker_id,
            why=why,
            timeout_seconds=self.timeout,
            queued=self.depth,
        )
        self._conn.request_close("write_timeout", code=1011, text="write_timeout")

    def _settle(
        self, done: "asyncio.Future[None] | None", error: BaseException | None
    ) -> None:
        if done is None or done.done():
            return
        if error is None:
            done.set_result(None)
        else:
            done.set_exception(error)

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                _payload, done = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._settle(done, error)
            if done is not None:
                with contextlib.suppress(Exception):
                    done.exception()
        self._observe()

    def _observe(self) -> None:
        if self.on_depth is not None:
            self.on_depth()


def _quiet(task: "asyncio.Task[None]") -> None:
    if not task.cancelled():
        task.exception()
