import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing, inject_traceparent
from apipi.services.event_bus import EventBus, create_event_bus, is_wake
from apipi.services.runtime import (
    continue_turn,
    fail_stale_in_progress,
    prepare_for_new_turn,
    request_cancel,
    run_turn,
)
from apipi.services.sink import DirectSink, OutboxSink, ResultSink
from apipi.store.blobs import (
    ArtifactBlobs,
    ObjectStore,
    ObjectStoreError,
    blob_store,
    object_store,
)
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import get_session, get_session_by_id, get_worker
from apipi.worker.pi.artifacts import harvest_session, reap_workspace_loop
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.isolation import load_isolation
from apipi.worker.pi.isolation.base import Isolation
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc

log = logging.getLogger("apipi.worker")


class LocalExecution:
    _SINK_CACHE_LIMIT = 4096

    def __init__(
        self,
        settings: Settings,
        *,
        pool: PiPool,
        harness: Any,
        isolation: Isolation,
        hub: EventBus,
        store: Store | None = None,
        blobs: ArtifactBlobs | None = None,
        objects: ObjectStore | None = None,
        metrics: Metrics | None = None,
        tracing: Tracing | None = None,
        outbox: Any | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.harness = harness
        self.isolation = isolation
        self.hub = hub
        self.store = store
        self.blobs = blobs
        self.objects = objects
        self.metrics = metrics
        self.tracing = tracing
        self.outbox = outbox
        self._sinks: dict[tuple[uuid.UUID, uuid.UUID], ResultSink] = {}
        self.note_stopped: Callable[[uuid.UUID], Awaitable[None]] | None = None
        self._context_ttl: dict[str, tuple[float | None, float, str | None]] = {}
        if pool.on_kill is None:
            pool.on_kill = self._harvest_killed
        if pool.on_transition is None:
            pool.on_transition = self._sandbox_transition

    def note_context_ttl(self, session_id: uuid.UUID, context: Any) -> None:
        """Remember the effective idle TTL from a command context."""
        if not isinstance(context, dict):
            return
        session = context.get("session")
        if not isinstance(session, dict):
            return
        seconds: float | None = None
        raw = session.get("idle_ttl_seconds")
        if isinstance(raw, (int, float)) and raw >= 0:
            seconds = float(raw)
        environment = session.get("environment")
        env_type = (
            environment.get("type")
            if isinstance(environment, dict)
            and isinstance(environment.get("type"), str)
            else None
        )
        self._context_ttl[str(session_id)] = (seconds, time.time(), env_type)

    def refresh_context_seen(self, session_id: uuid.UUID) -> None:
        """Restart the reaper idle clock after turn activity."""
        remembered = self._context_ttl.get(str(session_id))
        if remembered is None:
            return
        seconds, _seen, env_type = remembered
        self._context_ttl[str(session_id)] = (seconds, time.time(), env_type)

    def _forget_context(self, session_id: str) -> None:
        self._context_ttl.pop(session_id, None)

    def sink_for(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> ResultSink:
        """Per-session result sink; outbox-backed on a split worker."""
        key = (tenant_id, session_id)
        sink = self._sinks.get(key)
        if sink is None:
            if self.outbox is not None:
                sink = OutboxSink(
                    self.outbox,
                    tenant_id,
                    session_id,
                    settings=self.settings,
                    metrics=self.metrics,
                    tracing=self.tracing,
                )
            else:
                sink = DirectSink()
            if len(self._sinks) >= self._SINK_CACHE_LIMIT:
                self._sinks.pop(next(iter(self._sinks)))
            self._sinks[key] = sink
        return sink

    def drop_sink(self, session_id: uuid.UUID) -> None:
        for key in [key for key in self._sinks if key[1] == session_id]:
            del self._sinks[key]

    def attach_store(self, store: Store) -> None:
        self.store = store

    def capacity_code(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_mem_mib: int | None = None,
    ) -> str | None:
        return self.pool.capacity_code(
            session_id, tenant_id, session_mem_mib=session_mem_mib
        )

    def require(self) -> None:
        self.isolation.require(self.settings)

    async def probe(self) -> None:
        await self.isolation.probe(self.settings)

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
        sink: ResultSink | None = None,
    ) -> None:
        store = self.store
        assert store is not None
        self.note_context_ttl(session_id, turn_context)
        try:
            await run_turn(
                store,
                self.hub,
                self.harness,
                tenant_id,
                session_id,
                text,
                images=images,
                parts=parts,
                mcp_http=mcp_http,
                request_id=request_id,
                metrics=self.metrics,
                tracing=self.tracing,
                turn_timeout=self.settings.turn_timeout,
                settings=self.settings,
                pool=self.pool,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
                objects=self.objects,
                blobs=self.blobs,
                turn_context=turn_context,
                sink=sink if sink is not None else self.sink_for(tenant_id, session_id),
            )
        finally:
            self.refresh_context_seen(session_id)

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
        sink: ResultSink | None = None,
    ) -> None:
        store = self.store
        assert store is not None
        self.note_context_ttl(session_id, turn_context)
        try:
            await continue_turn(
                store,
                self.hub,
                self.harness,
                tenant_id,
                session_id,
                turn_id=turn_id,
                call_id=call_id,
                success=success,
                output=output,
                error=error,
                mcp_http=mcp_http,
                request_id=request_id,
                metrics=self.metrics,
                tracing=self.tracing,
                turn_timeout=self.settings.turn_timeout,
                settings=self.settings,
                pool=self.pool,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
                blobs=self.blobs,
                turn_context=turn_context,
                sink=sink if sink is not None else self.sink_for(tenant_id, session_id),
            )
        finally:
            self.refresh_context_seen(session_id)

    async def cancel(self, session_id: uuid.UUID, *, status: str) -> None:
        abort = request_cancel(self.hub, session_id, status=status)
        if abort is not None:
            abort.set()
        await self.harness.abort(session_id)

    async def prepare_for_new_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        store = self.store
        assert store is not None
        await prepare_for_new_turn(
            store,
            self.hub,
            self.harness,
            tenant_id,
            session_id,
            sink=self.sink_for(tenant_id, session_id),
        )

    async def teardown(self, session_id: uuid.UUID) -> None:
        self._forget_context(str(session_id))
        self.drop_sink(session_id)
        await self.pool.kill(session_id)

    async def reap_loop(self) -> None:
        await self.pool.reap_loop()

    async def reap_workspace_loop(self) -> None:
        store = self.store
        assert store is not None
        await reap_workspace_loop(
            self.settings,
            store,
            self.pool,
            ttl_overrides=self._context_ttl,
            on_wiped=self._forget_context,
        )

    async def observe_loop(self) -> None:
        interval = 5.0
        sample = self.settings.guest_sample_interval
        sample_every = sample.total_seconds() if sample is not None else None
        last_sample = 0.0
        while True:
            await self.pool.sweep_dead()
            await self.pool.enforce_memory()
            if self.metrics is not None:
                self.pool.refresh_metrics()
                self._observe_cgroup()
                self._observe_host_pi()
                now = asyncio.get_running_loop().time()
                if sample_every is not None and now - last_sample >= sample_every:
                    await self._observe_guest_samples()
                    last_sample = now
            await asyncio.sleep(interval)

    def _observe_cgroup(self) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        from apipi.worker.cgroup import read_cgroup

        totals: dict[str, dict[str, float]] = {
            size: {"memory_bytes": 0.0, "memory_limit_bytes": 0.0, "cpu_seconds": 0.0}
            for size in ("S", "M", "L")
        }
        for size, proc in self.pool.live_procs():
            if not proc.vm_id:
                continue
            data = read_cgroup(proc.vm_id)
            if data is None:
                continue
            bucket = totals[size]
            bucket["memory_bytes"] += data["memory_bytes"]
            bucket["memory_limit_bytes"] += data["memory_limit_bytes"]
            bucket["cpu_seconds"] += data["cpu_seconds"]
        for size, bucket in totals.items():
            metrics.set_guest_cgroup(size=size, **bucket)

    def _observe_host_pi(self) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        from apipi.worker.procmem import read_group_rss_pss

        count = 0
        rss = 0
        pss = 0
        for _size, proc in self.pool.live_procs():
            if proc.vm_id:
                continue
            process = getattr(proc, "process", None)
            pid = getattr(process, "pid", None)
            if pid is None:
                continue
            count += 1
            r, p = read_group_rss_pss(pid)
            rss += r
            pss += p
        metrics.set_host_pi(processes=count, rss_bytes=rss, pss_bytes=pss)

    async def _observe_guest_samples(self) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        totals: dict[str, dict[str, float]] = {
            size: {
                "n": 0.0,
                "mem_available_bytes": 0.0,
                "load": 0.0,
                "workspace_used_bytes": 0.0,
                "workspace_avail_bytes": 0.0,
            }
            for size in ("S", "M", "L")
        }
        for size, proc in self.pool.live_procs():
            pull = proc.pull_metrics
            if pull is None:
                continue
            try:
                raw = await pull()
                payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            bucket = totals[size]
            bucket["n"] += 1
            bucket["mem_available_bytes"] += float(
                payload.get("mem_available_bytes") or 0
            )
            bucket["load"] += float(payload.get("load_1") or 0)
            bucket["workspace_used_bytes"] += float(
                payload.get("workspace_used_bytes") or 0
            )
            bucket["workspace_avail_bytes"] += float(
                payload.get("workspace_avail_bytes") or 0
            )
        for size, bucket in totals.items():
            count = bucket["n"]
            metrics.set_guest_sample(
                size=size,
                mem_available_bytes=bucket["mem_available_bytes"],
                load=(bucket["load"] / count) if count else 0.0,
                workspace_used_bytes=bucket["workspace_used_bytes"],
                workspace_avail_bytes=bucket["workspace_avail_bytes"],
            )

    async def lifecycle_loop(self) -> None:
        from apipi.services.lifecycle_export import heartbeat_loop

        emitter = self.pool.lifecycle
        if emitter is None or emitter.heartbeat_s is None:
            return
        await heartbeat_loop(self.pool, emitter)

    async def sandbox_seen_loop(self) -> None:
        from apipi.services.sandbox_status import SEEN_INTERVAL, touch_seen

        while True:
            await asyncio.sleep(SEEN_INTERVAL.total_seconds())
            store = self.store
            if store is None:
                continue
            await touch_seen(store, self.pool.sandbox_seen_ids())

    async def _sandbox_transition(
        self, session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        store = self.store
        if store is None:
            return
        from apipi.services.sandbox_status import record_transition

        await record_transition(store, self.hub, session_id, phase, fields)

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
        self.note_context_ttl(session_id, turn_context)
        from apipi.config import CapacityError
        from apipi.env.setup import SetupError
        from apipi.services.runtime import fail_environment, load_boot_kwargs
        from apipi.store.blobs import ObjectStoreError

        sink = self.sink_for(tenant_id, session_id)
        try:
            try:
                kwargs = await load_boot_kwargs(
                    store,
                    self.settings,
                    tenant_id,
                    session_id,
                    mcp_http=mcp_http,
                    turn_context=turn_context,
                )
            except (SetupError, ApiError) as exc:
                code = exc.code if isinstance(exc, ApiError) and exc.code else None
                message = exc.message
                async with store.session() as db:
                    await fail_environment(
                        db,
                        self.hub,
                        tenant_id,
                        session_id,
                        message,
                        code=code,
                        sink=sink,
                    )
                return
            except ObjectStoreError:
                async with store.session() as db:
                    await fail_environment(
                        db,
                        self.hub,
                        tenant_id,
                        session_id,
                        "Cannot read artifacts",
                        code="artifact_store",
                        sink=sink,
                    )
                return
            if kwargs is None:
                return
            try:
                await self.pool.get(session_id, **kwargs)
            except CapacityError as exc:
                async with store.session() as db:
                    await fail_environment(
                        db,
                        self.hub,
                        tenant_id,
                        session_id,
                        str(exc),
                        code=exc.code,
                        sink=sink,
                    )
            except Exception:
                log.exception(
                    "sandbox boot failed", extra={"session_id": str(session_id)}
                )
                async with store.session() as db:
                    await fail_environment(
                        db,
                        self.hub,
                        tenant_id,
                        session_id,
                        "Computer failed to start",
                        code="internal",
                        sink=sink,
                    )
        finally:
            self.refresh_context_seen(session_id)

    async def close(self) -> None:
        await self.pool.close()
        emitter = self.pool.lifecycle
        if emitter is not None:
            await emitter.close()

    async def _harvest_killed(self, session_id: uuid.UUID, proc: PiProc | None) -> None:
        try:
            store = self.store
            if store is None:
                return
            async with store.session() as db:
                try:
                    await harvest_session(
                        db,
                        self.settings,
                        session_id,
                        proc,
                        sync_workspace=False,
                        blobs=self.blobs,
                    )
                except (OSError, ObjectStoreError):
                    return
                except asyncio.CancelledError:
                    return
        finally:
            note = self.note_stopped
            if note is not None:
                await note(session_id)


def local_execution(
    settings: Settings,
    *,
    store: Store,
    harness: Any | None = None,
    hub: EventBus | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    outbox: Any | None = None,
) -> LocalExecution:
    pool = PiPool(settings, tracing=tracing, metrics=metrics)
    from apipi.services.lifecycle_export import attach_lifecycle

    attach_lifecycle(pool, settings, metrics)
    isolation = load_isolation(settings.run_mode)
    resolved_harness = harness if harness is not None else PiHarness(pool)
    return LocalExecution(
        settings,
        pool=pool,
        harness=resolved_harness,
        isolation=isolation,
        hub=hub
        if hub is not None
        else create_event_bus(settings, store=store, metrics=metrics),
        store=store,
        blobs=blob_store(settings),
        objects=object_store(settings),
        metrics=metrics,
        tracing=tracing,
        outbox=outbox,
    )


def worker_observability(
    settings: Settings,
) -> tuple[Metrics | None, Tracing | None]:
    metrics = Metrics() if settings.metrics else None
    tracing = (
        Tracing(endpoint=settings.otel_endpoint) if settings.otel_endpoint else None
    )
    return metrics, tracing


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

    def require(self) -> None:
        return None

    async def probe(self) -> None:
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

    async def _wait(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        store = self.store
        assert store is not None
        done = {
            "agent.session.turn.completed",
            "agent.session.turn.failed",
            "agent.session.turn.cancelled",
            "agent.session.requires_action",
        }
        interval = max(self.settings.event_bus_fallback_poll.total_seconds(), 0.01)
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id)
        last = existing[-1].seq if existing else 0
        if any(event.type in done for event in existing):
            return
        queue = self.hub.subscribe(session_id)
        try:
            deadline = utc_now() + self.settings.turn_timeout
            while utc_now() < deadline:
                remaining = (deadline - utc_now()).total_seconds()
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=min(interval, max(remaining, 0.01))
                    )
                except TimeoutError:
                    message = None
                if message is not None:
                    if is_wake(message):
                        seq = message.get("seq")
                        if not isinstance(seq, int) or seq <= last:
                            continue
                    elif message.get("seq") is None or int(message["seq"]) <= last:
                        continue
                async with store.session() as db:
                    extra = await list_events(db, tenant_id, session_id, after_seq=last)
                if extra:
                    last = extra[-1].seq
                if any(event.type in done for event in extra):
                    return
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
        await self._wait(tenant_id, session_id)

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
        await self._wait(tenant_id, session_id)

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

    async def cancel(self, session_id: uuid.UUID, *, status: str) -> None:
        abort = request_cancel(self.hub, session_id, status=status)
        if abort is not None:
            abort.set()
        store = self.store
        if store is None:
            return
        async with store.session() as db:
            row = await get_session_by_id(db, session_id)
        if row is None or row.lease_id is None:
            return
        await self.workers.command(
            store, row.tenant_id, session_id, op="turn.cancel", payload={}
        )

    async def prepare_for_new_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        await self.cancel(session_id, status="in_progress")
        await self._wait(tenant_id, session_id)

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

    async def reap_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def reap_workspace_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def observe_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def lifecycle_loop(self) -> None:
        return None

    async def sandbox_seen_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

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
        from apipi.services.runtime import fail_environment

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
