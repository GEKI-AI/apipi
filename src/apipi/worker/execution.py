import asyncio
import json
import logging
import uuid
from typing import Any, NoReturn, Protocol

from apipi.config import Settings
from apipi.env.hub import EnvironmentHub
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing, inject_traceparent
from apipi.mcp.stdio import McpStdioServer
from apipi.services.runtime import (
    EventHub,
    continue_turn,
    fail_stale_in_progress,
    prepare_for_new_turn,
    request_cancel,
    run_turn,
)
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


class Execution(Protocol):
    stdio_on_host: bool

    def capacity_code(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_mem_mib: int | None = None,
    ) -> str | None: ...

    def put_stdio(
        self, session_id: uuid.UUID, servers: list[McpStdioServer]
    ) -> None: ...

    async def run_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        *,
        mcp_http: list[Any] | None = None,
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None: ...

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
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None: ...

    async def cancel(self, session_id: uuid.UUID, *, status: str) -> None: ...

    async def prepare_for_new_turn(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> None: ...

    async def teardown(self, session_id: uuid.UUID) -> None: ...

    def require(self) -> None: ...

    async def probe(self) -> None: ...

    async def reap_loop(self) -> None: ...

    async def reap_workspace_loop(self) -> None: ...

    async def observe_loop(self) -> None: ...

    async def close(self) -> None: ...


class LocalExecution:
    def __init__(
        self,
        settings: Settings,
        *,
        pool: PiPool,
        harness: Any,
        isolation: Isolation,
        hub: EventHub,
        env_hub: EnvironmentHub,
        store: Store | None = None,
        blobs: ArtifactBlobs | None = None,
        objects: ObjectStore | None = None,
        metrics: Metrics | None = None,
        tracing: Tracing | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.harness = harness
        self.isolation = isolation
        self.hub = hub
        self.env_hub = env_hub
        self.store = store
        self.blobs = blobs
        self.objects = objects
        self.metrics = metrics
        self.tracing = tracing
        if pool.on_kill is None:
            pool.on_kill = self._harvest_killed

    @property
    def stdio_on_host(self) -> bool:
        return self.isolation.stdio_on_host

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

    def put_stdio(self, session_id: uuid.UUID, servers: list[McpStdioServer]) -> None:
        self.pool.put_stdio(session_id, servers)

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
        mcp_http: list[Any] | None = None,
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None:
        store = self.store
        assert store is not None
        await run_turn(
            store,
            self.hub,
            self.harness,
            tenant_id,
            session_id,
            text,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            request_id=request_id,
            metrics=self.metrics,
            tracing=self.tracing,
            turn_timeout=self.settings.turn_timeout,
            env_hub=self.env_hub,
            settings=self.settings,
            pool=self.pool,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            thinking_summary=thinking_summary,
            auto_title=auto_title,
            objects=self.objects,
            blobs=self.blobs,
        )

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
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None:
        store = self.store
        assert store is not None
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
            mcp_stdio=mcp_stdio,
            request_id=request_id,
            metrics=self.metrics,
            tracing=self.tracing,
            turn_timeout=self.settings.turn_timeout,
            env_hub=self.env_hub,
            settings=self.settings,
            pool=self.pool,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            thinking_summary=thinking_summary,
            auto_title=auto_title,
            blobs=self.blobs,
        )

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
        await prepare_for_new_turn(store, self.hub, self.harness, tenant_id, session_id)

    async def teardown(self, session_id: uuid.UUID) -> None:
        await self.pool.kill(session_id)

    async def reap_loop(self) -> None:
        await self.pool.reap_loop()

    async def reap_workspace_loop(self) -> None:
        store = self.store
        assert store is not None
        await reap_workspace_loop(self.settings, store, self.pool)

    async def observe_loop(self) -> None:
        interval = 5.0
        sample = self.settings.guest_sample_interval
        sample_every = sample.total_seconds() if sample is not None else None
        last_sample = 0.0
        while True:
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

    async def close(self) -> None:
        await self.pool.close()

    async def _harvest_killed(self, session_id: uuid.UUID, proc: PiProc | None) -> None:
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
                    self.env_hub,
                    sync_workspace=False,
                    blobs=self.blobs,
                )
            except (OSError, ObjectStoreError):
                return
            except asyncio.CancelledError:
                return


def local_execution(
    settings: Settings,
    *,
    store: Store,
    harness: Any | None = None,
    hub: EventHub | None = None,
    env_hub: EnvironmentHub | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
) -> LocalExecution:
    pool = PiPool(settings, tracing=tracing, metrics=metrics)
    isolation = load_isolation(settings.run_mode)
    resolved_harness = harness if harness is not None else PiHarness(pool)
    return LocalExecution(
        settings,
        pool=pool,
        harness=resolved_harness,
        isolation=isolation,
        hub=hub if hub is not None else EventHub(),
        env_hub=env_hub if env_hub is not None else EnvironmentHub(),
        store=store,
        blobs=blob_store(settings),
        objects=object_store(settings),
        metrics=metrics,
        tracing=tracing,
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
    stdio_on_host = False

    def __init__(
        self,
        settings: Settings,
        *,
        workers: Any,
        store: Store | None,
        hub: EventHub,
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

    def put_stdio(self, session_id: uuid.UUID, servers: list[McpStdioServer]) -> None:
        del session_id, servers

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
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "tenant_id": str(tenant_id),
            "request_id": request_id,
            "api_key": api_key,
            "key_id": key_id,
            "user_id": user_id,
            "thinking_summary": thinking_summary,
            "auto_title": auto_title,
            **extra,
        }
        parent = inject_traceparent()
        if parent is not None:
            payload["traceparent"] = parent
        return payload

    async def _wait(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        store = self.store
        assert store is not None
        done = {
            "agent.session.turn.completed",
            "agent.session.turn.failed",
            "agent.session.turn.cancelled",
            "agent.session.requires_action",
        }
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id)
        last = existing[-1].seq if existing else 0
        deadline = utc_now() + self.settings.turn_timeout
        while utc_now() < deadline:
            async with store.session() as db:
                extra = await list_events(db, tenant_id, session_id, after_seq=last)
            if any(event.type in done for event in extra):
                return
            await asyncio.sleep(0.05)
        async with store.session() as db:
            await fail_stale_in_progress(db, self.hub, tenant_id, session_id)

    async def run_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        text: str,
        *,
        mcp_http: list[Any] | None = None,
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None:
        del mcp_http, mcp_stdio
        store = self.store
        assert store is not None
        sent = await self.workers.command(
            store,
            tenant_id,
            session_id,
            op="turn.start",
            payload=self._payload(
                tenant_id,
                {"text": text},
                request_id=request_id,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                thinking_summary=thinking_summary,
                auto_title=auto_title,
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
                    {"text": text},
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id,
                    user_id=user_id,
                    thinking_summary=thinking_summary,
                    auto_title=auto_title,
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
        mcp_stdio: list[Any] | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        thinking_summary: bool = False,
        auto_title: bool = False,
    ) -> None:
        del mcp_http, mcp_stdio
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
                },
                request_id=request_id,
                api_key=api_key,
                key_id=key_id,
                user_id=user_id,
                thinking_summary=thinking_summary,
                auto_title=auto_title,
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

    async def close(self) -> None:
        return None
