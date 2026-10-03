import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, NoReturn

from apipi.common.errors import ApiError
from apipi.common.event_bus import EventBus, is_wake, request_cancel
from apipi.common.logutil import log_event
from apipi.common.otel import inject_traceparent
from apipi.config import Settings
from apipi.services.turn_state import fail_stale_in_progress, lease_live
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    get_session,
    get_session_by_id,
    get_worker,
)

log = logging.getLogger("apipi.worker")

# How long a follow-up waits for a worker to acknowledge a cancel with
# events before treating the stale turn as abandoned.
CANCEL_GRACE = timedelta(seconds=5)


class RemoteExecution:
    def __init__(
        self,
        settings: Settings,
        *,
        workers: Any,
        store: Store | None,
        hub: EventBus,
    ) -> None:
        self.settings = settings
        self.workers = workers
        self.store = store
        self.hub = hub

    def attach_store(self, store: Store) -> None:
        self.store = store

    def capacity_code(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_mem_mib: int | None = None,
    ) -> str | None:
        del session_id, tenant_id, session_mem_mib
        if self.workers.live() == 0:
            return "capacity"
        return None

    def _payload(
        self,
        tenant_id: uuid.UUID,
        extra: dict[str, Any],
        *,
        request_id: str | None,
        api_key: str | None,
        key_id: str | None,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "tenant_id": str(tenant_id),
            "request_id": request_id,
            "api_key": api_key,
            "key_id": key_id,
            "user_id": user_id,
            "org_id": org_id,
            **extra,
        }
        parent = inject_traceparent()
        if parent is not None:
            payload["traceparent"] = parent
        return payload

    def _context_extra(self, turn_context: dict[str, Any] | None) -> dict[str, Any]:
        if turn_context is None:
            return {}
        return {"context": turn_context}

    async def _wait(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        baseline: int | None = None,
        *,
        timeout: timedelta | None = None,
    ) -> None:
        store = self.store
        assert store is not None
        terminal = {
            "agent.session.requires_action",
            "agent.session.failed",
        }
        # End-of-turn events whose status change travels in later
        # outbox envelopes (`session.status`, then the `idle` event).
        # Returning on the turn event alone races ingest: the POST would
        # report `in_progress` while the status envelope is still in
        # flight.
        idle_terminated = {
            "agent.session.turn.completed",
            "agent.session.turn.cancelled",
            "agent.session.turn.failed",
        }
        interval = max(self.settings.event_bus_fallback_poll.total_seconds(), 0.01)
        if baseline is None:
            async with store.session() as db:
                existing = await list_events(db, tenant_id, session_id)
            last = existing[-1].seq if existing else 0
        else:
            last = baseline
        queue = self.hub.subscribe(session_id)
        seen: set[str] = set()
        try:
            deadline = utc_now() + (timeout or self.settings.turn_timeout)
            while utc_now() < deadline:
                async with store.session() as db:
                    after = await list_events(db, tenant_id, session_id, after_seq=last)
                if after:
                    last = after[-1].seq
                    seen.update(event.type for event in after)
                    if any(item in terminal for item in seen):
                        return
                    # A finished turn is only done when the idle event
                    # that follows it is durable too; the turn event, the
                    # status change, and idle arrive as separate outbox
                    # envelopes, so wait for both after the baseline.
                    if (
                        idle_terminated.intersection(seen)
                        and "agent.session.idle" in seen
                    ):
                        return
                remaining = (deadline - utc_now()).total_seconds()
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=min(interval, max(remaining, 0.01))
                    )
                except TimeoutError:
                    continue
                if message is not None:
                    if is_wake(message):
                        seq = message.get("seq")
                        if not isinstance(seq, int) or seq <= last:
                            continue
                    elif message.get("seq") is None or int(message["seq"]) <= last:
                        continue
        finally:
            self.hub.unsubscribe(session_id, queue)
        async with store.session() as db:
            await fail_stale_in_progress(db, self.hub, tenant_id, session_id)

    async def run_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        *,
        images: list[dict[str, str]] | None = None,
        parts: list[dict[str, str]] | None = None,
        mcp_http: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        turn_context: dict[str, Any] | None = None,
    ) -> None:
        store = self.store
        assert store is not None
        async with store.session() as _db:
            _existing = await list_events(_db, tenant_id, session_id)
        _baseline = _existing[-1].seq if _existing else 0
        sent = await self.workers.command(
            store,
            tenant_id,
            session_id,
            op="turn.start",
            payload=self._payload(
                tenant_id,
                {
                    "text": text,
                    "images": images or [],
                    "parts": parts or [],
                    **self._context_extra(turn_context),
                },
                request_id=request_id,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
            ),
        )
        if sent is None:
            sent = await self.workers.acquire(
                store,
                tenant_id,
                session_id,
                op="turn.start",
                payload=self._payload(
                    tenant_id,
                    {
                        "text": text,
                        "images": images or [],
                        "parts": parts or [],
                        **self._context_extra(turn_context),
                    },
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id,
                    user_id=user_id,
                    org_id=org_id,
                ),
            )
        if sent is None:
            await self._raise_no_worker(tenant_id, session_id)
        await self._wait(tenant_id, session_id, _baseline)

    async def continue_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        turn_id: uuid.UUID,
        call_id: str,
        success: bool,
        output: str | None,
        error: str | None,
        mcp_http: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        turn_context: dict[str, Any] | None = None,
    ) -> None:
        store = self.store
        assert store is not None
        async with store.session() as _db:
            _existing = await list_events(_db, tenant_id, session_id)
        _baseline = _existing[-1].seq if _existing else 0
        sent = await self.workers.command(
            store,
            tenant_id,
            session_id,
            op="turn.continue",
            payload=self._payload(
                tenant_id,
                {
                    "turn_id": str(turn_id),
                    "call_id": call_id,
                    "success": success,
                    "output": output,
                    "error": error,
                    **self._context_extra(turn_context),
                },
                request_id=request_id,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
            ),
        )
        if sent is None:
            await self._raise_no_worker(tenant_id, session_id)
        await self._wait(tenant_id, session_id, _baseline)

    async def _raise_no_worker(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> NoReturn:
        store = self.store
        assert store is not None
        instance: str | None = None
        worker_id = None
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is not None and row.worker_id is not None:
                worker_id = row.worker_id
                worker = await get_worker(db, row.worker_id)
                if worker is not None:
                    instance = worker.api_instance_id
        missing = worker_id is not None and self.workers.get(worker_id) is None
        if missing:
            where = instance if instance else "another API process"
            log_event(
                log,
                logging.WARNING,
                "worker assign failed",
                event="worker.assign.failed",
                error_code="capacity",
                tenant_id=tenant_id,
                session_id=session_id,
                worker_id=worker_id,
            )
            raise ApiError(
                "invalid_request",
                f"Worker socket is on {where}",
                code="capacity",
                status_code=429,
            )
        log_event(
            log,
            logging.WARNING,
            "worker assign failed",
            event="worker.assign.failed",
            error_code="capacity",
            tenant_id=tenant_id,
            session_id=session_id,
            worker_id=worker_id,
        )
        raise ApiError(
            "invalid_request",
            "Too many live sessions",
            code="capacity",
            status_code=429,
        )

    async def cancel(self, session_id: uuid.UUID, *, status: str) -> bool:
        """Cancel a running turn; True when a live worker accepted it.

        False means no connected worker holds the session lease (e.g.
        the worker restarted), so the caller must not wait for worker
        events that will never arrive.
        """
        abort = request_cancel(self.hub, session_id, status=status)
        if abort is not None:
            abort.set()
        store = self.store
        if store is None:
            return False
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        if row is None or row.lease_id is None:
            return False
        command = await self.workers.command(
            store,
            row.tenant_id,
            session_id,
            op="turn.cancel",
            # dispatch_command keys the worker side on payload tenant_id;
            # without it the worker silently drops the cancel and a held
            # turn never aborts.
            payload={"tenant_id": str(row.tenant_id)},
        )
        return command is not None

    async def prepare_for_new_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        store = self.store
        assert store is not None
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        if row is None or row.lease_id is None:
            async with store.session() as db:
                await fail_stale_in_progress(db, self.hub, tenant_id, session_id)
            return
        lease_id = row.lease_id
        # Baseline before the cancel goes out, so its events are not missed.
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id)
        baseline = existing[-1].seq if existing else 0
        if not await self.cancel(session_id, status="in_progress"):
            # No command went out. If the lease holder's socket is on
            # another API replica and the lease is still live, the worker
            # may be running: answer with the usual 429 instead of
            # clearing a live lease. Otherwise the lease is orphaned
            # (expired, or the worker restarted without it): release it
            # and fail the stale turn instead of waiting for events that
            # will never arrive.
            if (
                row.worker_id is not None
                and self.workers.get(row.worker_id) is None
                and lease_live(row.lease_until)
            ):
                await self._raise_no_worker(tenant_id, session_id)
            await self._release_and_fail_stale(tenant_id, session_id, lease_id)
            return
        # The worker accepted the cancel, but if it has no running turn
        # for this session (stale `in_progress` row, lease still set) it
        # emits nothing. Bound the wait, then release the lease and fail
        # the stale turn instead of blocking until turn_timeout.
        await self._wait(
            tenant_id,
            session_id,
            baseline,
            timeout=min(CANCEL_GRACE, self.settings.turn_timeout),
        )
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        if row is not None and row.status == "in_progress":
            await self._release_and_fail_stale(tenant_id, session_id, lease_id)

    async def _release_and_fail_stale(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID, lease_id: uuid.UUID
    ) -> None:
        store = self.store
        assert store is not None
        # `release` clears only if the row still holds `lease_id`, and
        # drops the hub's per-connection lease bookkeeping.
        await self.workers.release(store, tenant_id, session_id, lease_id)
        async with store.session() as db:
            await fail_stale_in_progress(db, self.hub, tenant_id, session_id)

    async def teardown(self, session_id: uuid.UUID) -> None:
        store = self.store
        if store is None:
            return
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        if row is None or row.lease_id is None or row.worker_id is None:
            return
        command = await self.workers.command(
            store,
            row.tenant_id,
            session_id,
            op="session.stop",
            payload={"tenant_id": str(row.tenant_id)},
        )
        if command is not None:
            await self.workers.wait_ack(row.lease_id, str(command["id"]))
        await self.workers.release(store, row.tenant_id, session_id, row.lease_id)

    async def boot_hosted(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        mcp_http: list[Any] | None = None,
        turn_context: dict[str, Any] | None = None,
    ) -> None:
        store = self.store
        if store is None:
            return
        from apipi.services.turn_state import fail_environment

        payload = {"tenant_id": str(tenant_id), **self._context_extra(turn_context)}
        sent = await self.workers.command(
            store,
            tenant_id,
            session_id,
            op="sandbox.boot",
            payload=payload,
        )
        if sent is None:
            sent = await self.workers.acquire(
                store,
                tenant_id,
                session_id,
                op="sandbox.boot",
                payload=payload,
            )
        if sent is None:
            async with store.session() as db:
                await fail_environment(
                    db,
                    self.hub,
                    tenant_id,
                    session_id,
                    "No worker available",
                    code="capacity",
                )

    async def close(self) -> None:
        return None
