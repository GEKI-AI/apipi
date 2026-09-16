import asyncio
import logging
import uuid
from typing import Any, NoReturn, Protocol

from apipi.blobs import ArtifactBlobs
from apipi.config import Settings
from apipi.env.hub import EnvironmentHub
from apipi.errors import ApiError
from apipi.mcp.stdio import McpStdioServer
from apipi.metrics import Metrics
from apipi.otel import Tracing
from apipi.pi.artifacts import harvest_session, reap_workspace_loop
from apipi.pi.harness import PiHarness
from apipi.pi.isolation import load_isolation
from apipi.pi.isolation.base import Isolation
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.runtime import (
    EventHub,
    continue_turn,
    fail_stale_in_progress,
    prepare_for_new_turn,
    request_cancel,
    run_turn,
)
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import get_session, get_session_by_id, get_worker

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

    async def close(self) -> None:
        await self.pool.close()

    async def _harvest_killed(self, session_id: uuid.UUID, proc: PiProc | None) -> None:
        store = self.store
        if store is None:
            return
        async with store.session() as db:
            await harvest_session(
                db,
                self.settings,
                session_id,
                proc,
                self.env_hub,
                sync_workspace=False,
                blobs=self.blobs,
            )


def local_execution(
    settings: Settings,
    *,
    store: Store,
    harness: Any | None = None,
    hub: EventHub | None = None,
    env_hub: EnvironmentHub | None = None,
) -> LocalExecution:
    pool = PiPool(settings)
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
    )


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
    ) -> dict[str, Any]:
        payload = {
            "tenant_id": str(tenant_id),
            "request_id": request_id,
            "api_key": api_key,
            "key_id": key_id,
            **extra,
        }
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
            log.warning(
                "worker socket missing",
                extra={
                    "session_id": str(session_id),
                    "worker_id": str(worker_id),
                    "api_instance_id": instance,
                },
            )
            raise ApiError(
                "invalid_request",
                f"Worker socket is on {where}",
                code="capacity",
                status_code=429,
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
        if row is None or row.lease_id is None:
            return
        await self.workers.release(store, row.tenant_id, session_id, row.lease_id)

    async def reap_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def reap_workspace_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def close(self) -> None:
        return None
