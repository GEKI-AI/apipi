import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI

from apipi.api.agents import router as agents_router
from apipi.api.environments import router as environments_router
from apipi.api.files import router as files_router
from apipi.api.health import router as health_router
from apipi.api.models import router as models_router
from apipi.api.sessions import router as sessions_router
from apipi.api.skills import router as skills_router
from apipi.api.usage import router as usage_router
from apipi.api.vaults import router as vaults_router
from apipi.api.workers import router as workers_router
from apipi.config import Settings, load_settings
from apipi.env.hub import EnvironmentHub
from apipi.gateway.auth import AuthCache, Authenticate, load_authenticate
from apipi.gateway.errors import register_exception_handlers
from apipi.gateway.logutil import RequestLogMiddleware
from apipi.gateway.metrics import Metrics, mount_metrics
from apipi.gateway.middleware import InstanceMiddleware, MaxBodyMiddleware
from apipi.gateway.otel import Tracing
from apipi.gateway.request_id import RequestIdMiddleware
from apipi.pi.harness import PiHarness
from apipi.pi.isolation import load_isolation
from apipi.pi.isolation.base import Isolation
from apipi.pi.pool import PiPool
from apipi.services.agents import AgentService
from apipi.services.files import FileService
from apipi.services.models import ModelsService
from apipi.services.payload_export import load_payload_sinks
from apipi.services.runtime import EventHub, FakeHarness
from apipi.services.sessions import SessionService
from apipi.services.skill_store import SkillService
from apipi.services.usage_export import load_usage_sinks
from apipi.services.usage_service import UsageService
from apipi.services.vaults import VaultService
from apipi.store.blobs import ArtifactAdapter, ArtifactBlobs, ObjectStore, object_store
from apipi.store.engine import Store, create_engine
from apipi.store.models import Tenant, utc_now
from apipi.store.repo import ensure_tenant as store_ensure_tenant
from apipi.store.repo import purge_turn_logs
from apipi.worker import WorkerHub
from apipi.worker.execution import LocalExecution, RemoteExecution


async def _purge_usage_loop(settings: Settings, store: Store) -> None:
    while True:
        await asyncio.sleep(3600)
        if settings.usage_retention is None:
            continue
        cutoff = utc_now() - settings.usage_retention
        async with store.session() as db:
            await purge_turn_logs(db, cutoff)


@dataclass(frozen=True)
class GatewayRouters:
    sessions: APIRouter
    agents: APIRouter
    vaults: APIRouter
    files: APIRouter
    skills: APIRouter
    environments: APIRouter
    usage: APIRouter
    models: APIRouter
    workers: APIRouter
    health: APIRouter


class Gateway:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        store_owned: bool,
        event_hub: EventHub,
        env_hub: EnvironmentHub,
        execution: LocalExecution | RemoteExecution,
        workers: WorkerHub,
        authenticate: Authenticate,
        isolation: Isolation,
        pool: PiPool,
        harness: FakeHarness | PiHarness,
        blobs: ArtifactBlobs,
        objects: ObjectStore,
        metrics: Metrics | None,
        tracing: Tracing | None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.event_hub = event_hub
        self.env_hub = env_hub
        self.execution = execution
        self.workers = workers
        self.authenticate = authenticate
        self.isolation = isolation
        self.pool = pool
        self.harness = harness
        self.blobs = blobs
        self.objects = objects
        self.metrics = metrics
        self.tracing = tracing
        self.mcp_http: dict[uuid.UUID, Any] = {}
        self.mcp_stdio: dict[uuid.UUID, Any] = {}
        self.files = FileService(store, objects, settings)
        self.skill_store = SkillService(store, objects, settings)
        self.sessions = SessionService(
            settings=settings,
            store=store,
            event_hub=event_hub,
            env_hub=env_hub,
            execution=execution,
            blobs=blobs,
            files=self.files,
            skill_store=self.skill_store,
            tracing=tracing,
            mcp_http=self.mcp_http,
            mcp_stdio=self.mcp_stdio,
        )
        self.agents = AgentService(store)
        self.vaults = VaultService(store)
        self.usage = UsageService(store)
        self.models = ModelsService(settings)
        self.routers = GatewayRouters(
            sessions=sessions_router,
            agents=agents_router,
            vaults=vaults_router,
            files=files_router,
            skills=skills_router,
            environments=environments_router,
            usage=usage_router,
            models=models_router,
            workers=workers_router,
            health=health_router,
        )
        self._store_owned = store_owned
        self._auth_cache = AuthCache(settings.auth_cache_ttl)
        self._usage_sinks = load_usage_sinks(settings, metrics)
        self._payload_sinks = load_payload_sinks(settings, metrics)
        self._tasks: list[asyncio.Task[None]] = []

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        store: Store | None = None,
        harness: FakeHarness | PiHarness | None = None,
        pool: PiPool | None = None,
        tracing: Tracing | None = None,
        blobs: ArtifactBlobs | None = None,
        *,
        authenticate: Authenticate | None = None,
        event_hub: EventHub | None = None,
        execution: LocalExecution | RemoteExecution | None = None,
        workers: WorkerHub | None = None,
    ) -> "Gateway":
        resolved = settings if settings is not None else load_settings()
        store_owned = store is None
        resolved_store = (
            store
            if store is not None
            else Store(
                create_engine(
                    resolved.database_url,
                    pool_size=resolved.db_pool_size,
                )
            )
        )
        resolved_pool = pool if pool is not None else PiPool(resolved)
        isolation = load_isolation(resolved.run_mode)
        resolved_harness = harness if harness is not None else PiHarness(resolved_pool)
        hub = event_hub if event_hub is not None else EventHub()
        env_hub = EnvironmentHub()
        resolved_objects = object_store(resolved)
        resolved_blobs = (
            blobs if blobs is not None else ArtifactAdapter(resolved_objects)
        )
        resolved_metrics = Metrics() if resolved.metrics else None
        if tracing is not None:
            resolved_tracing = tracing
        elif resolved.otel_endpoint:
            resolved_tracing = Tracing(endpoint=resolved.otel_endpoint)
        else:
            resolved_tracing = None
        resolved_workers = (
            workers
            if workers is not None
            else WorkerHub(resolved, metrics=resolved_metrics)
        )
        if execution is not None:
            resolved_execution = execution
        elif resolved.api_only:
            resolved_execution = RemoteExecution(
                resolved, workers=resolved_workers, store=resolved_store, hub=hub
            )
        else:
            resolved_execution = LocalExecution(
                resolved,
                pool=resolved_pool,
                harness=resolved_harness,
                isolation=isolation,
                hub=hub,
                env_hub=env_hub,
                store=resolved_store,
                blobs=resolved_blobs,
                objects=resolved_objects,
                metrics=resolved_metrics,
                tracing=resolved_tracing,
            )
        auth = (
            authenticate
            if authenticate is not None
            else load_authenticate(resolved.auth)
        )
        return cls(
            settings=resolved,
            store=resolved_store,
            store_owned=store_owned,
            event_hub=hub,
            env_hub=env_hub,
            execution=resolved_execution,
            workers=resolved_workers,
            authenticate=auth,
            isolation=isolation,
            pool=resolved_pool,
            harness=resolved_harness,
            blobs=resolved_blobs,
            objects=resolved_objects,
            metrics=resolved_metrics,
            tracing=resolved_tracing,
        )

    async def ensure_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        async with self.store.session() as db:
            return await store_ensure_tenant(db, tenant_id)

    def configure(self, app: FastAPI) -> None:
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(InstanceMiddleware, instance_id=self.settings.instance_id)
        app.add_middleware(
            MaxBodyMiddleware,
            max_bytes=self.settings.max_request_bytes,
            file_max_bytes=int(self.settings.max_file_bytes),
        )
        app.add_middleware(RequestLogMiddleware)
        app.state.gateway = self
        app.state.settings = self.settings
        app.state.isolation = self.isolation
        app.state.metrics = self.metrics
        app.state.tracing = self.tracing
        app.state.store = self.store
        app.state.mcp_http = self.mcp_http
        app.state.mcp_stdio = self.mcp_stdio
        app.state.sessions = self.sessions
        app.state.authenticate = self.authenticate
        app.state.auth_cache = self._auth_cache
        app.state.usage_sinks = self._usage_sinks
        app.state.payload_sinks = self._payload_sinks
        app.state.event_hub = self.event_hub
        app.state.env_hub = self.env_hub
        app.state.pi_pool = self.pool
        app.state.harness = self.harness
        app.state.execution = self.execution
        app.state.workers = self.workers
        app.state.blobs = self.blobs
        app.state.objects = self.objects
        register_exception_handlers(app)

    async def startup(self) -> None:
        self.execution.attach_store(self.store)
        self._tasks = [
            asyncio.create_task(self.execution.reap_loop()),
            asyncio.create_task(self.execution.reap_workspace_loop()),
            asyncio.create_task(_purge_usage_loop(self.settings, self.store)),
            asyncio.create_task(self._expire_worker_leases()),
        ]

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        await self.execution.close()
        if isinstance(self.tracing, Tracing):
            self.tracing.shutdown()
        if self._store_owned:
            await self.store.dispose()

    async def _expire_worker_leases(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.workers.expire(self.store, self.event_hub)


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    harness: FakeHarness | PiHarness | None = None,
    pool: PiPool | None = None,
    tracing: Tracing | None = None,
    blobs: ArtifactBlobs | None = None,
    *,
    authenticate: Authenticate | None = None,
    event_hub: EventHub | None = None,
    execution: LocalExecution | RemoteExecution | None = None,
    workers: WorkerHub | None = None,
) -> FastAPI:
    gateway = Gateway.create(
        settings,
        store=store,
        harness=harness,
        pool=pool,
        tracing=tracing,
        blobs=blobs,
        authenticate=authenticate,
        event_hub=event_hub,
        execution=execution,
        workers=workers,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = FastAPI(title="ApiPi", version="0.0.0", lifespan=lifespan)
    gateway.configure(app)
    app.include_router(gateway.routers.sessions)
    app.include_router(gateway.routers.vaults)
    app.include_router(gateway.routers.files)
    app.include_router(gateway.routers.skills)
    app.include_router(gateway.routers.agents)
    app.include_router(gateway.routers.environments)
    app.include_router(gateway.routers.usage)
    app.include_router(gateway.routers.models)
    app.include_router(gateway.routers.workers)
    app.include_router(gateway.routers.health)
    if isinstance(gateway.metrics, Metrics):
        mount_metrics(app, gateway.metrics)
    return app
