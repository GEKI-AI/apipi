import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI

from apipi.api.agents import router as agents_router
from apipi.api.auth import router as auth_router
from apipi.api.environments import router as environments_router
from apipi.api.files import router as files_router
from apipi.api.health import router as health_router
from apipi.api.models import router as models_router
from apipi.api.sessions import router as sessions_router
from apipi.api.skills import router as skills_router
from apipi.api.templates import router as templates_router
from apipi.api.uploads import router as uploads_router
from apipi.api.usage import router as usage_router
from apipi.api.vaults import router as vaults_router
from apipi.api.workers import router as workers_router
from apipi.common.background import spawn_loop, start_event_loop_lag, watch_task
from apipi.common.event_bus import EventBus
from apipi.common.metrics import Metrics
from apipi.common.otel import Tracing
from apipi.config import VAULT_MASTER_KEY_UNSET, Settings, load_settings
from apipi.gateway.auth import (
    AuthCache,
    Authenticate,
    AuthIdentity,
    Authorize,
    load_authenticate,
    load_authorize,
)
from apipi.gateway.errors import register_exception_handlers
from apipi.gateway.metrics import mount_metrics
from apipi.gateway.middleware import InstanceMiddleware, MaxBodyMiddleware
from apipi.gateway.request_id import RequestIdMiddleware
from apipi.gateway.request_log import RequestLogMiddleware
from apipi.services.agents import AgentService
from apipi.services.event_bus import create_event_bus
from apipi.services.files import FileService
from apipi.services.lifecycle_export import LifecycleEmitter, create_lifecycle
from apipi.services.model_credentials import (
    ModelCredential,
    ModelCredentials,
    load_model_credential,
)
from apipi.services.models import ModelsService
from apipi.services.payload_export import load_payload_sinks
from apipi.services.search import SearchResolver, SearchService
from apipi.services.sessions import SessionService
from apipi.services.skill_store import SkillService
from apipi.services.templates import TemplateService
from apipi.services.uploads import UploadService
from apipi.services.usage_export import load_usage_sinks
from apipi.services.usage_service import UsageService
from apipi.services.vault_crypto import vault_master_key_unset
from apipi.services.vaults import VaultService
from apipi.store.blobs import ArtifactAdapter, ArtifactBlobs, ObjectStore, object_store
from apipi.store.engine import Store, create_engine
from apipi.store.models import Tenant, utc_now
from apipi.store.repo import ensure_tenant as store_ensure_tenant
from apipi.store.repo import purge_turn_logs
from apipi.workerhub.execution import RemoteExecution
from apipi.workerhub.hub import WorkerHub

log = logging.getLogger("apipi")


async def _purge_usage_once(settings: Settings, store: Store) -> None:
    if settings.usage_retention is None:
        return
    cutoff = utc_now() - settings.usage_retention
    async with store.session() as db:
        await purge_turn_logs(db, cutoff)


@dataclass(frozen=True)
class GatewayRouters:
    sessions: APIRouter
    agents: APIRouter
    vaults: APIRouter
    files: APIRouter
    uploads: APIRouter
    skills: APIRouter
    templates: APIRouter
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
        event_hub: EventBus,
        execution: RemoteExecution,
        workers: WorkerHub,
        authenticate: Authenticate,
        authorize: Authorize | None = None,
        model_credential: ModelCredential | None = None,
        lifecycle: LifecycleEmitter | None,
        blobs: ArtifactBlobs,
        objects: ObjectStore,
        metrics: Metrics | None,
        tracing: Tracing | None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.event_hub = event_hub
        self.execution = execution
        self.workers = workers
        self.authenticate = authenticate
        self.authorize = authorize
        self.model_credentials = ModelCredentials(settings, model_credential)
        self.lifecycle = lifecycle
        self.blobs = blobs
        self.objects = objects
        self.metrics = metrics
        self.tracing = tracing
        self.files = FileService(store, objects, settings)
        self.skill_store = SkillService(store, objects, settings)
        self.uploads = UploadService(store, objects, settings)
        self.search_resolver = SearchResolver(settings)
        self.search = SearchService(
            store, settings, self.search_resolver, metrics=metrics
        )
        self.sessions = SessionService(
            settings=settings,
            store=store,
            event_hub=event_hub,
            execution=execution,
            blobs=blobs,
            files=self.files,
            skill_store=self.skill_store,
            tracing=tracing,
            metrics=metrics,
            search=self.search_resolver,
            model_credentials=self.model_credentials,
        )
        self.agents = AgentService(store, settings, self.search_resolver)
        self.templates = TemplateService(
            store,
            objects,
            settings,
            self.agents,
            self.files,
            self.skill_store,
        )
        self.vaults = VaultService(store, settings)
        self.usage = UsageService(store)
        self.models = ModelsService(settings)
        self.routers = GatewayRouters(
            sessions=sessions_router,
            agents=agents_router,
            vaults=vaults_router,
            files=files_router,
            uploads=uploads_router,
            skills=skills_router,
            templates=templates_router,
            environments=environments_router,
            usage=usage_router,
            models=models_router,
            workers=workers_router,
            health=health_router,
        )
        self._store_owned = store_owned
        self._auth_cache = AuthCache(
            settings.auth_cache_ttl, max_entries=settings.auth_cache_max
        )
        from collections import OrderedDict as _OrderedDict

        self._tenant_cache: _OrderedDict[uuid.UUID, Tenant] = _OrderedDict()
        self._usage_sinks = load_usage_sinks(settings, metrics)
        self._payload_sinks = load_payload_sinks(settings, metrics)
        self._tasks: list[asyncio.Task[None]] = []

    def invalidate_auth(self, cache_key: str) -> bool:
        from apipi.gateway.tokens import hash_token as _hash

        return self._auth_cache.invalidate(_hash(cache_key))

    def invalidate_auth_where(self, predicate: Callable[[AuthIdentity], bool]) -> int:
        return self._auth_cache.invalidate_where(predicate)

    def clear_auth_cache(self) -> int:
        return self._auth_cache.clear()

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        store: Store | None = None,
        tracing: Tracing | None = None,
        blobs: ArtifactBlobs | None = None,
        objects: ObjectStore | None = None,
        *,
        authenticate: Authenticate | None = None,
        authorize: Authorize | None = None,
        model_credential: ModelCredential | None = None,
        event_hub: EventBus | None = None,
        execution: RemoteExecution | None = None,
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
        resolved_objects = objects if objects is not None else object_store(resolved)
        resolved_blobs = (
            blobs if blobs is not None else ArtifactAdapter(resolved_objects)
        )
        resolved_metrics = Metrics() if resolved.metrics else None
        hub = (
            event_hub
            if event_hub is not None
            else create_event_bus(
                resolved, store=resolved_store, metrics=resolved_metrics
            )
        )
        if tracing is not None:
            resolved_tracing = tracing
        elif resolved.otel_endpoint:
            resolved_tracing = Tracing(endpoint=resolved.otel_endpoint)
        else:
            resolved_tracing = None
        # The API owns the lifecycle export: it emits from worker
        # envelopes and inventories.
        resolved_lifecycle = create_lifecycle(resolved, resolved_metrics)
        resolved_workers = (
            workers
            if workers is not None
            else WorkerHub(resolved, metrics=resolved_metrics, tracing=resolved_tracing)
        )
        resolved_execution = (
            execution
            if execution is not None
            else RemoteExecution(
                resolved, workers=resolved_workers, store=resolved_store, hub=hub
            )
        )
        auth = (
            authenticate
            if authenticate is not None
            else load_authenticate(resolved.auth)
        )
        resolved_authorize = (
            authorize if authorize is not None else load_authorize(resolved.authorize)
        )
        resolved_credential = (
            model_credential
            if model_credential is not None
            else load_model_credential(resolved.model_credential)
        )
        return cls(
            settings=resolved,
            store=resolved_store,
            store_owned=store_owned,
            event_hub=hub,
            execution=resolved_execution,
            workers=resolved_workers,
            authenticate=auth,
            authorize=resolved_authorize,
            model_credential=resolved_credential,
            lifecycle=resolved_lifecycle,
            blobs=resolved_blobs,
            objects=resolved_objects,
            metrics=resolved_metrics,
            tracing=resolved_tracing,
        )

    async def ensure_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        cached = self._tenant_cache.get(tenant_id)
        if cached is not None:
            self._tenant_cache.move_to_end(tenant_id)
            return cached
        async with self.store.session() as db:
            tenant = await store_ensure_tenant(db, tenant_id)
            detached = Tenant(id=tenant.id, name=tenant.name)
            limit = (
                max(1, self.settings.auth_cache_max)
                if self.settings.auth_cache_max
                else 10000
            )
            self._tenant_cache[tenant_id] = detached
            while len(self._tenant_cache) > limit:
                self._tenant_cache.popitem(last=False)
            return detached

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
        app.state.lifecycle = self.lifecycle
        app.state.metrics = self.metrics
        app.state.tracing = self.tracing
        app.state.store = self.store
        app.state.sessions = self.sessions
        app.state.search = self.search
        app.state.authenticate = self.authenticate
        app.state.authorize = self.authorize
        app.state.model_credentials = self.model_credentials
        app.state.auth_cache = self._auth_cache
        app.state.usage_sinks = self._usage_sinks
        app.state.payload_sinks = self._payload_sinks
        app.state.event_hub = self.event_hub
        app.state.execution = self.execution
        app.state.workers = self.workers
        app.state.blobs = self.blobs
        app.state.objects = self.objects
        register_exception_handlers(app)

    async def startup(self) -> None:
        if vault_master_key_unset(self.settings.vault_master_key):
            log.warning(VAULT_MASTER_KEY_UNSET)
        self.execution.attach_store(self.store)
        await self.event_hub.start()
        await self.workers.start_forwarding(
            self.store,
            self.event_hub,
            context_factory=self.sessions.forward_context,
            image_factory=self.sessions.forward_image_parts,
            stop_local=self.execution.stop_local,
        )
        emitter = self.lifecycle
        if emitter is not None:
            emitter.start()
        self._tasks = [
            start_event_loop_lag(self.metrics),
            spawn_loop(
                "usage_purge",
                lambda: _purge_usage_once(self.settings, self.store),
                interval=3600,
                metrics=self.metrics,
            ),
            spawn_loop(
                "attachment_sweep",
                self.files.sweep_attachments,
                interval=3600,
                metrics=self.metrics,
            ),
            spawn_loop(
                "lease_reaper",
                self._expire_worker_leases,
                interval=1,
                metrics=self.metrics,
            ),
            spawn_loop(
                "worker_forwards",
                self.workers.poll_forwards,
                interval=max(
                    self.settings.event_bus_fallback_poll.total_seconds(), 0.05
                ),
                metrics=self.metrics,
            ),
            spawn_loop(
                "command_retransmit",
                self._retransmit_worker_commands,
                interval=1,
                metrics=self.metrics,
            ),
        ]
        if emitter is not None:
            from apipi.services.lifecycle_export import api_heartbeat_loop

            heartbeat = asyncio.create_task(
                api_heartbeat_loop(self.settings, emitter, self.workers, self.store),
                name="lifecycle_heartbeat",
            )
            watch_task(heartbeat, "lifecycle_heartbeat", metrics=self.metrics)
            self._tasks.append(heartbeat)

    async def shutdown(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.execution.close()
        if self.lifecycle is not None:
            await self.lifecycle.close()
        await self.sessions.cancel_turns()
        await self.workers.stop_forwarding()
        await self.search.aclose()
        await self.event_hub.close()
        if isinstance(self.tracing, Tracing):
            self.tracing.shutdown()
        if self._store_owned:
            await self.store.dispose()

    async def _expire_worker_leases(self) -> None:
        await self.workers.expire(self.store, self.event_hub)

    async def _retransmit_worker_commands(self) -> None:
        await self.workers.retransmit_due(self.store, self.event_hub)


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    tracing: Tracing | None = None,
    blobs: ArtifactBlobs | None = None,
    objects: ObjectStore | None = None,
    *,
    authenticate: Authenticate | None = None,
    authorize: Authorize | None = None,
    model_credential: ModelCredential | None = None,
    event_hub: EventBus | None = None,
    execution: RemoteExecution | None = None,
    workers: WorkerHub | None = None,
) -> FastAPI:
    gateway = Gateway.create(
        settings,
        store=store,
        tracing=tracing,
        blobs=blobs,
        objects=objects,
        authenticate=authenticate,
        authorize=authorize,
        model_credential=model_credential,
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
    app.include_router(gateway.routers.uploads)
    app.include_router(gateway.routers.skills)
    app.include_router(gateway.routers.templates)
    app.include_router(gateway.routers.agents)
    app.include_router(auth_router)
    app.include_router(gateway.routers.environments)
    app.include_router(gateway.routers.usage)
    app.include_router(gateway.routers.models)
    app.include_router(gateway.routers.workers)
    app.include_router(gateway.routers.health)
    if isinstance(gateway.metrics, Metrics):
        mount_metrics(app, gateway.metrics)
    return app
