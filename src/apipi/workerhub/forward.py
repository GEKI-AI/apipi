"""Commands for a worker whose socket is on another API replica.

The replica that takes a request stores one `worker_forwards` row (the
request without its context) and sends the id to the replica that
holds the worker socket, over the event bus. That replica claims the
row, builds the context from the database, vault, and object store,
sends the command through its own hub, and writes the outcome back to
the row. The row is the durable part: a lost notification is repaired by
a resend while the caller waits and by a poll on the receiving side. The
design is in `specs/decisions/0015-worker-protocol-v2.md`.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from apipi.common.errors import ApiError
from apipi.common.event_bus import (
    InstanceBus,
    forward_message,
    forward_result_message,
)
from apipi.common.logutil import RateLimitedLog, log_event
from apipi.protocol import (
    COMMAND_PAYLOAD_MODELS,
    BaseCommandPayload,
    ContextCommandPayload,
)
from apipi.store.engine import Store
from apipi.store.models import WorkerForward, utc_now
from apipi.store.repo import (
    claim_worker_forward,
    create_worker_forward,
    delete_worker_forward,
    get_worker_forward,
    list_pending_worker_forwards,
    purge_worker_forwards,
    set_worker_forward_status,
)

if TYPE_CHECKING:
    from apipi.workerhub.hub import WorkerHub

log = logging.getLogger("apipi.worker")

SENT_TIMEOUT = 10.0
ACK_TIMEOUT = 15.0
STOP_TIMEOUT = 15.0
RESULT_POLL = 1.0
ROW_TTL = 600.0
PURGE_INTERVAL = 60.0
CONTEXT_OPS = frozenset({"turn.start", "turn.continue", "sandbox.boot"})
STRIPPED_FIELDS = ("context", "run_mode", "sandbox_image", "last_seq")

ContextFactory = Callable[..., Awaitable[dict[str, Any] | None]]
ImageFactory = Callable[..., Awaitable[list[dict[str, Any]]]]
StopLocal = Callable[..., Awaitable[None]]


def unreachable(message: str) -> ApiError:
    return ApiError("api_error", message, code="worker_unreachable", status_code=503)


def forward_timeout(message: str) -> ApiError:
    return ApiError("api_error", message, code="forward_timeout", status_code=504)


def failure_reason(code: str | None) -> str:
    if code == "worker_unreachable":
        return "not_connected"
    if code == "forward_timeout":
        return "timeout"
    if code == "command_ack_timeout":
        return "ack_timeout"
    if code in (None, "", "forward_failed"):
        return "error"
    return "rejected"


def forward_body(
    op: str, payload: BaseCommandPayload | dict[str, Any] | None
) -> dict[str, Any]:
    """The part of a command that is stored: the payload without its context."""
    model = (
        payload
        if isinstance(payload, BaseCommandPayload)
        else COMMAND_PAYLOAD_MODELS[op].model_validate(payload or {})
    )
    data = model.to_wire()
    has_context = data.get("context") is not None
    for key in STRIPPED_FIELDS:
        data.pop(key, None)
    parts = data.get("parts")
    if isinstance(parts, list):
        data["parts"] = [_forward_part(part) for part in parts]
    return {"payload": data, "has_context": has_context}


def _forward_part(part: Any) -> Any:
    """An image part without its store reference; the owner signs it again."""
    if isinstance(part, dict) and part.get("type") == "image":
        return {"type": "image", "file_id": part.get("file_id")}
    return part


def _has_image(parts: Any) -> bool:
    return isinstance(parts, list) and any(
        isinstance(part, dict) and part.get("type") == "image" for part in parts
    )


class Forwarder:
    """Both ends of a forward: ask another replica, and serve what it asked."""

    def __init__(
        self,
        hub: "WorkerHub",
        store: Store,
        bus: InstanceBus,
        *,
        context_factory: ContextFactory | None = None,
        image_factory: ImageFactory | None = None,
        stop_local: StopLocal | None = None,
        sent_timeout: float = SENT_TIMEOUT,
        poll_interval: float = RESULT_POLL,
    ) -> None:
        self.hub = hub
        self.store = store
        self.bus = bus
        self.context_factory = context_factory
        self.image_factory = image_factory
        self.stop_local = stop_local
        self.sent_timeout = sent_timeout
        self.poll_interval = poll_interval
        self._waiting: dict[uuid.UUID, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._warnings = RateLimitedLog(log)
        self._purged = time.monotonic()

    async def start(self) -> None:
        await self.bus.listen_instance(self.hub.instance_id, self._on_message)

    async def close(self) -> None:
        await self.bus.unlisten_instance(self.hub.instance_id)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _on_message(self, message: dict[str, Any]) -> None:
        try:
            forward_id = uuid.UUID(str(message.get("id")))
        except ValueError:
            return
        kind = message.get("kind")
        if kind == "forward_result":
            event = self._waiting.get(forward_id)
            if event is not None:
                event.set()
        elif kind == "forward":
            self._spawn(forward_id)

    def _spawn(self, forward_id: uuid.UUID) -> None:
        task = asyncio.get_running_loop().create_task(self._process(forward_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def poll(self) -> None:
        """Pick up rows whose notification was lost, and drop old rows."""
        async with self.store.session() as db:
            pending = await list_pending_worker_forwards(
                db, target=self.hub.instance_id
            )
        for forward_id in pending:
            self._spawn(forward_id)
        if time.monotonic() - self._purged >= PURGE_INTERVAL:
            self._purged = time.monotonic()
            async with self.store.session() as db:
                await purge_worker_forwards(db, utc_now() - timedelta(seconds=ROW_TTL))

    async def call(
        self,
        target: str,
        *,
        action: str,
        op: str,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        worker_id: uuid.UUID,
        body: dict[str, Any],
        wait: str,
        forward_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Store a forward, notify its owner, and wait as asked.

        Returns the forward id. A failure on the other side is raised as
        the `ApiError` a local send would have raised.
        """
        forward_id = forward_id or uuid.uuid4()
        started = time.monotonic()
        async with self.store.session() as db:
            await create_worker_forward(
                db,
                WorkerForward(
                    id=forward_id,
                    action=action,
                    op=op,
                    wait=wait,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    worker_id=worker_id,
                    target=target,
                    origin=self.hub.instance_id,
                    body=body,
                    status="pending",
                ),
            )
        event = asyncio.Event()
        self._waiting[forward_id] = event
        try:
            await self._notify(target, forward_id)
            if wait == "none":
                self._observe(op, started, None, result="queued")
                return forward_id
            result = await self._await_result(target, forward_id, wait, event)
            if result.status == "failed":
                raise self._error_of(result)
        except ApiError as exc:
            self._observe(op, started, exc)
            await self._remove(forward_id)
            raise
        finally:
            self._waiting.pop(forward_id, None)
        self._observe(op, started, None)
        await self._remove(forward_id)
        return forward_id

    async def _notify(self, target: str, forward_id: uuid.UUID) -> None:
        await self.bus.send_instance(
            target, forward_message(forward_id, origin=self.hub.instance_id)
        )

    def _timeout_for(self, wait: str) -> float:
        extra = {"ack": ACK_TIMEOUT, "stopped": ACK_TIMEOUT + STOP_TIMEOUT}
        return self.sent_timeout + extra.get(wait, 0.0)

    async def _read(self, forward_id: uuid.UUID) -> WorkerForward | None:
        async with self.store.session() as db:
            return await get_worker_forward(db, forward_id)

    async def _await_result(
        self,
        target: str,
        forward_id: uuid.UUID,
        wait: str,
        event: asyncio.Event,
    ) -> WorkerForward:
        started = time.monotonic()
        deadline = started + self._timeout_for(wait)
        while True:
            row = await self._read(forward_id)
            if row is None:
                raise forward_timeout("The forward was lost before it finished")
            if row.status in ("done", "failed"):
                return row
            now = time.monotonic()
            if row.status == "pending" and now - started >= self.sent_timeout:
                if await self._withdraw(forward_id):
                    raise forward_timeout(
                        f"The API replica {target} did not pick up the command"
                    )
                continue
            if now >= deadline:
                raise forward_timeout(
                    f"The API replica {target} did not finish the command in time"
                )
            if row.status == "pending":
                await self._notify(target, forward_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    event.wait(),
                    timeout=min(self.poll_interval, max(deadline - now, 0.01)),
                )
            event.clear()

    async def _withdraw(self, forward_id: uuid.UUID) -> bool:
        """Fail a forward nobody claimed; False when the owner claimed it meanwhile."""
        async with self.store.session() as db:
            return await set_worker_forward_status(
                db,
                forward_id,
                "failed",
                code="forward_timeout",
                message="not picked up",
                http_status=504,
                only_from="pending",
            )

    async def _remove(self, forward_id: uuid.UUID) -> None:
        try:
            async with self.store.session() as db:
                await delete_worker_forward(db, forward_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._warnings.warning(
                "forward row not removed",
                event="worker.forward.cleanup_failed",
                error_code="forward_cleanup_failed",
            )

    def _error_of(self, row: WorkerForward) -> ApiError:
        status = row.http_status or 503
        kind = "invalid_request" if 400 <= status < 500 else "api_error"
        return ApiError(
            kind,
            row.message or "The forwarded command failed",
            code=row.code or "forward_failed",
            status_code=status,
            session_id=str(row.session_id),
        )

    def _observe(
        self,
        op: str,
        started: float,
        error: ApiError | None,
        *,
        result: str = "ok",
    ) -> None:
        seconds = time.monotonic() - started
        if error is not None:
            result = "timeout" if error.code == "forward_timeout" else "failed"
        metrics = self.hub.metrics
        if metrics is not None:
            metrics.observe_worker_forward(op, result, seconds)
            if error is not None:
                metrics.observe_worker_forward_failure(failure_reason(error.code))
        if error is not None:
            self._warnings.warning(
                "worker command forward failed",
                event="worker.forward.failed",
                error_code=error.code or "forward_failed",
                key=f"failed:{error.code}",
                op=op,
                reason=failure_reason(error.code),
                duration_seconds=round(seconds, 3),
            )

    async def _process(self, forward_id: uuid.UUID) -> None:
        async with self.store.session() as db:
            row = await claim_worker_forward(
                db, forward_id, target=self.hub.instance_id
            )
        if row is None:
            return
        started = time.monotonic()
        status, code, message, http_status = "done", None, None, None
        try:
            await self._execute(row)
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            status, code, message = "failed", exc.code or "forward_failed", exc.message
            http_status = exc.status_code
        except Exception as exc:
            log_event(
                log,
                logging.ERROR,
                "forwarded command failed",
                event="worker.forward.error",
                error_code="forward_failed",
                exc_info=exc,
                forward_id=forward_id,
                op=row.op,
                session_id=row.session_id,
            )
            status, code, http_status = "failed", "forward_failed", 500
            message = "The forwarded command failed"
        log_event(
            log,
            logging.INFO if status == "done" else logging.WARNING,
            "forwarded command handled",
            event="worker.forward.handled",
            error_code=code,
            forward_id=forward_id,
            op=row.op,
            action=row.action,
            session_id=row.session_id,
            origin=row.origin,
            result=status,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        if row.wait == "none":
            await self._remove(forward_id)
            return
        async with self.store.session() as db:
            await set_worker_forward_status(
                db,
                forward_id,
                status,
                code=code,
                message=message,
                http_status=http_status,
            )
        await self.bus.send_instance(
            row.origin, forward_result_message(forward_id, status)
        )

    async def _execute(self, row: WorkerForward) -> None:
        hub = self.hub
        body = row.body if isinstance(row.body, dict) else {}
        if row.action == "revoke":
            await hub.end_lease_local(
                row.session_id,
                row.worker_id,
                uuid.UUID(str(body["lease_id"])),
                send=bool(body.get("send", True)),
            )
            return
        payload = await self._payload(row, body)
        if row.action == "acquire":
            wire = await hub.acquire(
                self.store,
                row.tenant_id,
                row.session_id,
                op=row.op,
                payload=payload,
                command_id=row.id,
                pin_worker=row.worker_id,
            )
            if wire is None:
                raise ApiError(
                    "invalid_request",
                    "Too many live sessions",
                    code="capacity",
                    status_code=429,
                )
            return
        if row.op == "session.stop" and row.wait == "stopped":
            if self.stop_local is None:
                raise unreachable("This API replica cannot stop sessions")
            await self.stop_local(
                row.tenant_id,
                row.session_id,
                row.worker_id,
                command_id=row.id,
            )
            return
        wire = await hub.command(
            self.store,
            row.tenant_id,
            row.session_id,
            op=row.op,
            payload=payload,
            command_id=row.id,
            local_only=True,
        )
        if wire is None:
            raise unreachable("The worker is not connected to this API replica")
        if row.wait == "ack" and not await hub.wait_ack(
            uuid.UUID(str(wire["lease_id"])), str(row.id)
        ):
            raise ApiError(
                "api_error",
                "The worker did not acknowledge the command",
                code="command_ack_timeout",
                status_code=504,
            )

    async def _payload(
        self, row: WorkerForward, body: dict[str, Any]
    ) -> BaseCommandPayload:
        data = dict(body.get("payload") or {})
        if row.op == "turn.start" and _has_image(data.get("parts")):
            if self.image_factory is None:
                raise unreachable("This API replica cannot build image references")
            data["parts"] = await self.image_factory(row.tenant_id, data["parts"])
        model = COMMAND_PAYLOAD_MODELS[row.op].model_validate(data)
        if (
            body.get("has_context")
            and row.op in CONTEXT_OPS
            and isinstance(model, ContextCommandPayload)
        ):
            if self.context_factory is None:
                raise unreachable("This API replica cannot build a turn context")
            context = await self.context_factory(
                row.tenant_id,
                row.session_id,
                key_id=model.key_id,
                user_id=model.user_id,
                org_id=model.org_id,
            )
            model = model.model_copy(update={"context": context})
        return model
