import uuid
from typing import Any, Protocol

from apipi.blobs import ArtifactBlobs
from apipi.config import Settings
from apipi.env.hub import EnvironmentHub
from apipi.mcp.stdio import McpStdioServer
from apipi.metrics import Metrics
from apipi.otel import Tracing
from apipi.pi.artifacts import harvest_session, reap_workspace_loop
from apipi.pi.isolation.base import Isolation
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.runtime import (
    EventHub,
    continue_turn,
    prepare_for_new_turn,
    request_cancel,
    run_turn,
)
from apipi.store.engine import Store


class Execution(Protocol):
    stdio_on_host: bool

    def capacity_code(
        self, session_id: uuid.UUID, tenant_id: uuid.UUID
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

    def capacity_code(self, session_id: uuid.UUID, tenant_id: uuid.UUID) -> str | None:
        return self.pool.capacity_code(session_id, tenant_id)

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
