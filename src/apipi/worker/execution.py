import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, NoReturn

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing, inject_traceparent
from apipi.services.event_bus import EventBus, InMemoryEventBus, is_wake
from apipi.services.runtime import (
    continue_turn,
    fail_stale_in_progress,
    lease_live,
    prepare_for_new_turn,
    request_cancel,
    run_turn,
)
from apipi.services.sink import OutboxSink, ResultSink
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    get_session,
    get_session_by_id,
    get_worker,
)
from apipi.worker.pi.artifacts import reap_workspace_loop
from apipi.worker.pi.broker import SearchHookError
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc
from apipi.worker.protocol import SearchRequest

log = logging.getLogger("apipi.worker")

# How long a follow-up waits for a worker to acknowledge a cancel with
# events before treating the stale turn as abandoned.
CANCEL_GRACE = timedelta(seconds=5)

SEARCH_TIMEOUT = 30.0

SearchSender = Callable[[dict[str, Any]], Awaitable[None]]


def context_web_search(turn_context: dict[str, Any] | None) -> bool:
    if not isinstance(turn_context, dict):
        return False
    agent = turn_context.get("agent")
    return isinstance(agent, dict) and agent.get("web_search") is True


class _SearchHarness:
    """Adds the search flag and hook to every generate call of one turn."""

    def __init__(self, inner: Any, search: Any) -> None:
        self._inner = inner
        self._search = search

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.generate(
            *args, web_search=True, search=self._search, **kwargs
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LocalExecution:
    _SINK_CACHE_LIMIT = 4096

    def __init__(
        self,
        settings: Settings,
        *,
        pool: PiPool,
        harness: Any,
        hub: EventBus,
        outbox: Any,
        metrics: Metrics | None = None,
        tracing: Tracing | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.harness = harness
        self.hub = hub
        self.metrics = metrics
        self.tracing = tracing
        self.outbox = outbox
        self.presign_waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
        self.search_waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]] = {}
        self.search_sender: SearchSender | None = None
        self.search_timeout = SEARCH_TIMEOUT
        self._sinks: dict[tuple[uuid.UUID, uuid.UUID], ResultSink] = {}
        self.note_stopped: Callable[[uuid.UUID], Awaitable[None]] | None = None
        self.seen_hook: Callable[[list[uuid.UUID]], Awaitable[None]] | None = None
        self._context_ttl: dict[str, tuple[float | None, float, str | None]] = {}
        self._session_dirs: dict[str, str] = {}
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
        env = environment if isinstance(environment, dict) else {}
        raw_type = env.get("type")
        env_type = raw_type if isinstance(raw_type, str) else None
        self._context_ttl[str(session_id)] = (seconds, time.time(), env_type)
        # Remember the hosted workspace directory too, so the killed
        # harvest can find it without any database read.
        raw_dir = env.get("directory")
        if env_type == "openai_hosted" and isinstance(raw_dir, str) and raw_dir:
            self._session_dirs[str(session_id)] = raw_dir
        else:
            self._session_dirs.pop(str(session_id), None)

    def refresh_context_seen(self, session_id: uuid.UUID) -> None:
        """Restart the reaper idle clock after turn activity."""
        remembered = self._context_ttl.get(str(session_id))
        if remembered is None:
            return
        seconds, _seen, env_type = remembered
        self._context_ttl[str(session_id)] = (seconds, time.time(), env_type)

    def _forget_context(self, session_id: str) -> None:
        self._context_ttl.pop(session_id, None)
        self._session_dirs.pop(session_id, None)

    def _turn_harness(self, turn_context: dict[str, Any] | None) -> Any:
        if context_web_search(turn_context):
            return _SearchHarness(self.harness, self.search)
        return self.harness

    async def search(
        self,
        session_id: str,
        turn_id: str,
        query: str,
        max_results: int | None,
    ) -> dict[str, Any]:
        """Send one `search.request` and wait for its `search.reply`.

        It never queues and never replays: no socket, a lost socket or a
        timeout raises `SearchHookError`, which the broker turns into a
        tool error for the model.
        """
        sender = self.search_sender
        if sender is None:
            raise SearchHookError(
                "search_unavailable", "Web search is not available right now"
            )
        request_id = uuid.uuid4()
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError:
            raise SearchHookError(
                "invalid_request", "Web search request is invalid"
            ) from None
        acked = await self.outbox.wait_acked(
            session_uuid, self.outbox.high_water(session_uuid), timeout=5.0
        )
        if not acked:
            raise SearchHookError(
                "search_unavailable", "Web search is not available right now"
            )
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.search_waiters[request_id] = future
        try:
            request = SearchRequest(
                request_id=request_id,
                session_id=uuid.UUID(session_id),
                turn_id=uuid.UUID(turn_id),
                query=query,
                max_results=max_results,
            )
            try:
                await sender(request.model_dump(mode="json"))
            except Exception as exc:
                raise SearchHookError(
                    "search_unavailable", "Web search is not available right now"
                ) from exc
            try:
                return await asyncio.wait_for(future, timeout=self.search_timeout)
            except TimeoutError as exc:
                raise SearchHookError("search_timeout", "Web search timed out") from exc
        finally:
            self.search_waiters.pop(request_id, None)

    def handle_search_reply(self, message: dict[str, Any]) -> None:
        try:
            request_id = uuid.UUID(str(message.get("request_id")))
        except ValueError:
            return
        future = self.search_waiters.get(request_id)
        if future is not None and not future.done():
            future.set_result(message)

    def fail_search_waiters(self, message: str = "Worker connection lost") -> None:
        for future in list(self.search_waiters.values()):
            if not future.done():
                future.set_exception(SearchHookError("search_unavailable", message))
        self.search_waiters.clear()

    def sink_for(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> ResultSink:
        """Per-session result sink backed by the worker outbox."""
        key = (tenant_id, session_id)
        sink = self._sinks.get(key)
        if sink is None:
            sink = OutboxSink(
                self.outbox,
                tenant_id,
                session_id,
                settings=self.settings,
                metrics=self.metrics,
                tracing=self.tracing,
                waiters=self.presign_waiters,
            )
            if len(self._sinks) >= self._SINK_CACHE_LIMIT:
                self._sinks.pop(next(iter(self._sinks)))
            self._sinks[key] = sink
        return sink

    def drop_sink(self, session_id: uuid.UUID) -> None:
        for key in [key for key in self._sinks if key[1] == session_id]:
            del self._sinks[key]

    def capacity_code(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_mem_mib: int | None = None,
    ) -> str | None:
        return self.pool.capacity_code(
            session_id, tenant_id, session_mem_mib=session_mem_mib
        )

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
        self.note_context_ttl(session_id, turn_context)
        try:
            await run_turn(
                self.hub,
                self._turn_harness(turn_context),
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
        self.note_context_ttl(session_id, turn_context)
        try:
            await continue_turn(
                self.hub,
                self._turn_harness(turn_context),
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
        del tenant_id
        await prepare_for_new_turn(self.hub, self.harness, session_id)

    async def teardown(self, session_id: uuid.UUID) -> None:
        self._forget_context(str(session_id))
        self.drop_sink(session_id)
        await self.pool.kill(session_id)

    async def reap_loop(self) -> None:
        await self.pool.reap_loop()

    async def reap_workspace_loop(self) -> None:
        def on_wiped(session_id: str) -> None:
            self._forget_context(session_id)
            try:
                self.outbox.append(
                    uuid.UUID(session_id), "workspace.reaped", {"reason": "idle"}
                )
            except Exception:
                log.exception(
                    "workspace reaped report failed",
                    extra={"session_id": session_id},
                )

        await reap_workspace_loop(
            self.settings,
            self.pool,
            ttl_overrides=self._context_ttl,
            on_wiped=on_wiped,
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

    async def sandbox_seen_loop(self) -> None:
        from apipi.services.sandbox_status import SEEN_INTERVAL

        while True:
            await asyncio.sleep(SEEN_INTERVAL.total_seconds())
            seen = self.pool.sandbox_seen_ids()
            if self.seen_hook is None:
                continue
            try:
                await self.seen_hook(seen)
            except Exception:
                log.exception("sandbox seen report failed")

    async def _sandbox_transition(
        self, session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        payload: dict[str, Any] = {"status": phase}
        for key, value in fields.items():
            if isinstance(value, uuid.UUID):
                payload[key] = str(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
        try:
            self.outbox.append(session_id, "sandbox.status", payload)
        except Exception:
            log.exception(
                "sandbox status report failed",
                extra={"session_id": str(session_id)},
            )

    async def boot_hosted(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        mcp_http: list[Any] | None = None,
        turn_context: dict[str, Any] | None = None,
    ) -> None:
        self.note_context_ttl(session_id, turn_context)
        from apipi.config import CapacityError
        from apipi.env.setup import SetupError
        from apipi.services.runtime import load_boot_kwargs, report_environment_failed
        from apipi.store.blobs import ObjectStoreError

        sink = self.sink_for(tenant_id, session_id)

        async def fail(message: str, code: str | None) -> None:
            await report_environment_failed(
                sink, self.hub, tenant_id, session_id, message, code=code
            )

        try:
            try:
                kwargs = await load_boot_kwargs(
                    self.settings,
                    tenant_id,
                    session_id,
                    mcp_http=mcp_http,
                    turn_context=turn_context,
                )
            except (SetupError, ApiError) as exc:
                code = exc.code if isinstance(exc, ApiError) and exc.code else None
                await fail(exc.message, code)
                return
            except ObjectStoreError:
                await fail("Cannot read artifacts", "artifact_store")
                return
            if kwargs is None:
                return
            if context_web_search(turn_context):
                kwargs["web_search"] = True
            try:
                await self.pool.get(session_id, **kwargs)
            except CapacityError as exc:
                await fail(str(exc), exc.code)
            except Exception:
                log.exception(
                    "sandbox boot failed", extra={"session_id": str(session_id)}
                )
                await fail("Computer failed to start", "internal")
        finally:
            self.refresh_context_seen(session_id)

    async def close(self) -> None:
        await self.pool.close()
        emitter = self.pool.lifecycle
        if emitter is not None:
            await emitter.close()

    async def _harvest_killed(self, session_id: uuid.UUID, proc: PiProc | None) -> None:
        try:
            await self._upload_killed(session_id, proc)
        finally:
            note = self.note_stopped
            if note is not None:
                await note(session_id)

    async def _upload_killed(self, session_id: uuid.UUID, proc: PiProc | None) -> None:
        """Upload a killed session's files through presigned URLs.

        Tenant identity comes from the live sinks and the workspace
        directory from the remembered turn context.
        """
        from pathlib import Path as _Path

        from apipi.worker.artifact_upload import upload_via_presign
        from apipi.worker.pi.artifacts import (
            _hosted_files,
            read_pi_session_bytes,
        )

        tenant_id: uuid.UUID | None = None
        for tenant, sid in list(self._sinks.keys()):
            if sid == session_id:
                tenant_id = tenant
                break
        if tenant_id is None:
            return
        dest: Any | None = None
        raw_dir = self._session_dirs.get(str(session_id))
        if raw_dir:
            dest = _Path(raw_dir)
        files: list[tuple[str, bytes]] = []
        try:
            hosted, _workspace_error = await _hosted_files(
                proc,
                dest,
                sync_workspace=False,
                max_workspace_bytes=self.settings.max_workspace_bytes,
            )
            files = hosted
            if not files and dest is not None:
                from apipi.worker.pi.artifacts import (
                    read_workspace_artifacts as _read_ws,
                )

                try:
                    files = _read_ws(dest)
                except Exception:
                    files = []
        except Exception:
            files = []
        for rel, data in files:
            try:
                await upload_via_presign(
                    self.outbox,
                    self.presign_waiters,
                    self.settings,
                    session_id,
                    kind="artifact",
                    filename=rel,
                    content_type=None,
                    data=data,
                )
            except Exception:
                return
        try:
            pi_data = await read_pi_session_bytes(proc, dest)
        except Exception:
            pi_data = b""
        if pi_data:
            try:
                await upload_via_presign(
                    self.outbox,
                    self.presign_waiters,
                    self.settings,
                    session_id,
                    kind="pi_session",
                    filename="pi-session.jsonl",
                    content_type="application/octet-stream",
                    data=pi_data,
                )
            except Exception:
                return


def local_execution(
    settings: Settings,
    *,
    outbox: Any,
    harness: Any | None = None,
    hub: EventBus | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
) -> LocalExecution:
    pool = PiPool(settings, tracing=tracing, metrics=metrics)
    resolved_harness = harness if harness is not None else PiHarness(pool)
    return LocalExecution(
        settings,
        pool=pool,
        harness=resolved_harness,
        hub=hub if hub is not None else InMemoryEventBus(),
        outbox=outbox,
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
