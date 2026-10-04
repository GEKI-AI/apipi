import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from apipi.common.background import run_loop
from apipi.common.errors import ApiError
from apipi.common.event_bus import EventBus, InMemoryEventBus, request_cancel
from apipi.common.logutil import RateLimitedLog, log_event
from apipi.common.metrics import Metrics
from apipi.common.otel import Tracing
from apipi.config import Settings
from apipi.protocol import (
    SandboxStatusPayload,
    SearchReply,
    SearchRequest,
    WorkspaceReapedPayload,
)
from apipi.worker.pi.artifacts import reap_workspace_loop
from apipi.worker.pi.broker import SearchHookError
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc
from apipi.worker.runtime import continue_turn, prepare_for_new_turn, run_turn
from apipi.worker.sink import OutboxSink, ResultSink

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
        self.socket_open = True
        self.search_timeout = SEARCH_TIMEOUT
        self._warnings = RateLimitedLog(log)
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
                await sender(request.to_wire())
            except Exception as exc:
                raise SearchHookError(
                    "search_unavailable", "Web search is not available right now"
                ) from exc
            try:
                reply = await asyncio.wait_for(future, timeout=self.search_timeout)
            except TimeoutError as exc:
                self._note_waiter("search", "timeout", session_id=session_id)
                raise SearchHookError("search_timeout", "Web search timed out") from exc
            except SearchHookError:
                self._note_waiter("search", "disconnected", session_id=session_id)
                raise
            self._note_waiter("search", "ok", session_id=session_id)
            return reply
        finally:
            self.search_waiters.pop(request_id, None)

    def _note_waiter(self, kind: str, result: str, *, session_id: str) -> None:
        if self.metrics is not None:
            self.metrics.observe_worker_waiter(kind, result)
        if result == "timeout":
            self._warnings.warning(
                "worker request timed out",
                event="worker.waiter.timeout",
                error_code="waiter_timeout",
                kind=kind,
                session_id=session_id,
            )

    def handle_search_reply(self, message: dict[str, Any]) -> None:
        try:
            reply = SearchReply.model_validate(message)
        except ValidationError:
            return
        future = self.search_waiters.get(reply.request_id)
        if future is not None and not future.done():
            future.set_result(reply.to_wire())

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
        parts: list[dict[str, Any]] | None = None,
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
                    uuid.UUID(session_id),
                    "workspace.reaped",
                    WorkspaceReapedPayload(reason="idle"),
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
            metrics=self.metrics,
        )

    async def observe_loop(self) -> None:
        sample = self.settings.guest_sample_interval
        sample_every = sample.total_seconds() if sample is not None else None
        last_sample = 0.0

        async def observe_round() -> None:
            nonlocal last_sample
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

        await run_loop(
            "worker_observe",
            observe_round,
            interval=5.0,
            metrics=self.metrics,
            immediate=True,
        )

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
        from apipi.protocol import SEEN_INTERVAL

        async def seen_round() -> None:
            seen = self.pool.sandbox_seen_ids()
            if self.seen_hook is None:
                return
            try:
                await self.seen_hook(seen)
            except Exception:
                log.exception("sandbox seen report failed")

        await run_loop(
            "sandbox_seen",
            seen_round,
            interval=SEEN_INTERVAL.total_seconds(),
            metrics=self.metrics,
        )

    async def _sandbox_transition(
        self, session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        payload: dict[str, Any] = {"status": phase}
        for key, value in fields.items():
            if key not in SandboxStatusPayload.model_fields:
                continue
            if isinstance(value, uuid.UUID):
                payload[key] = str(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
        try:
            self.outbox.append(
                session_id,
                "sandbox.status",
                SandboxStatusPayload.model_validate(payload),
            )
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
        from apipi.common.errors import ObjectStoreError
        from apipi.config import CapacityError
        from apipi.env.setup import SetupError
        from apipi.worker.runtime import load_boot_kwargs, report_environment_failed

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
        if not self.socket_open:
            log_event(
                log,
                logging.INFO,
                "worker harvest skipped, no socket",
                event="worker.harvest.skipped",
                session_id=session_id,
            )
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
