import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from apipi.config import CapacityError, Settings
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing, start_span
from apipi.mcp.http import McpHttpServer
from apipi.services.lifecycle_export import LifecycleEmitter, utc_ts
from apipi.worker.pi.proc import PiProc, spawn_pi
from apipi.worker.pi.sandbox import size_for_mem

_EVENT_REASON = {
    "session": "stop",
    "idle": "idle",
    "respawn": "respawn",
    "memory": "memory",
    "crash": "crash",
    "drain": "drain",
    "shutdown": "shutdown",
}

OnKill = Callable[[uuid.UUID, PiProc | None], Awaitable[None]]
OnTransition = Callable[[uuid.UUID, str, dict[str, Any]], Awaitable[None]]
log = logging.getLogger("apipi.worker.pi")


def _hosted(env_type: str | None) -> bool:
    return env_type == "openai_hosted"


def _consume_future(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    future.exception()


class PiPool:
    def __init__(
        self,
        settings: Settings,
        *,
        on_kill: OnKill | None = None,
        tracing: Tracing | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.settings = settings
        self.on_kill = on_kill
        self.tracing = tracing
        self.metrics = metrics
        self._procs: dict[uuid.UUID, PiProc] = {}
        self._tenants: dict[uuid.UUID, uuid.UUID] = {}
        self._last: dict[uuid.UUID, float] = {}
        self._spawn_tools: dict[uuid.UUID, bool] = {}
        self._models: dict[uuid.UUID, str | None] = {}
        self._instructions: dict[uuid.UUID, str | None] = {}
        self._thinking: dict[uuid.UUID, str] = {}
        self._codemodes: dict[uuid.UUID, str] = {}
        self._web_search: dict[uuid.UUID, bool] = {}
        self._system_prompts: dict[uuid.UUID, str | None] = {}
        self._ttls: dict[uuid.UUID, float | None] = {}
        self._key_ids: dict[uuid.UUID, str | None] = {}
        self._env_types: dict[uuid.UUID, str | None] = {}
        self._mem: dict[uuid.UUID, int] = {}
        self._sizes: dict[uuid.UUID, str] = {}
        self._born: dict[uuid.UUID, float] = {}
        self._live: dict[uuid.UUID, dict[str, Any]] = {}
        self._held: set[uuid.UUID] = set()
        self.lifecycle: LifecycleEmitter | None = None
        self.on_transition: OnTransition | None = None
        self._booting: set[uuid.UUID] = set()
        self._reserved: dict[uuid.UUID, tuple[uuid.UUID | None, int]] = {}
        self._inflight: dict[uuid.UUID, asyncio.Future[PiProc]] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        session_id: uuid.UUID,
        *,
        cwd: str | None,
        tools: bool,
        mcp_http: list[McpHttpServer] | None = None,
        function_tools: list[dict[str, Any]] | None = None,
        skill_dirs: list[str] | None = None,
        tenant_id: uuid.UUID | None = None,
        model: str | None = None,
        instructions: str | None = None,
        api_key: str | None = None,
        key_id: str | None = None,
        env_type: str | None = None,
        mem_mib: int | None = None,
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        thinking: str | None = None,
        system_prompt: str | None = None,
        system_prompt_set: bool = False,
        codemode: str = "off",
        web_search: bool = False,
        idle_ttl: timedelta | None = None,
        idle_ttl_set: bool = False,
        agent_id: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> PiProc:
        instructions = instructions if instructions else None
        from apipi.worker.pi.settings_json import process_system_prompt

        level = thinking if thinking is not None else self.settings.pi_thinking
        prompt = (
            system_prompt if system_prompt_set else process_system_prompt(self.settings)
        )
        session_mem = mem_mib if mem_mib is not None else self.settings.microvm_mem_mib
        cause = "spawn"
        while True:
            wait_started = time.monotonic()
            async with self._lock:
                lock_wait_ms = int((time.monotonic() - wait_started) * 1000)
                inflight = self._inflight.get(session_id)
                if inflight is None:
                    current = self._procs.get(session_id)
                    if current is not None and not current.alive:
                        await self.kill(session_id, reason="crash")
                    elif (
                        current is not None
                        and current.alive
                        and not self._same(
                            session_id,
                            tools=tools,
                            model=model,
                            instructions=instructions,
                            level=level,
                            prompt=prompt,
                            key_id=key_id,
                            codemode=codemode,
                            web_search=web_search,
                        )
                    ):
                        await self.kill(session_id, reason="respawn")
                        cause = "respawn"
                    ready = self._claim_or_reuse(
                        session_id,
                        tools=tools,
                        model=model,
                        instructions=instructions,
                        level=level,
                        prompt=prompt,
                        key_id=key_id,
                        tenant_id=tenant_id,
                        session_mem=session_mem,
                        env_type=env_type,
                        idle_ttl=idle_ttl,
                        idle_ttl_set=idle_ttl_set,
                        lock_wait_ms=lock_wait_ms,
                    )
                    if not isinstance(ready, str):
                        return ready
                    future: asyncio.Future[PiProc] = (
                        asyncio.get_running_loop().create_future()
                    )
                    self._inflight[session_id] = future
                    self._reserved[session_id] = (tenant_id, session_mem)
                    self._booting.add(session_id)
                    break
            try:
                await inflight
            except Exception:
                continue
        size = size_for_mem(self.settings, session_mem)
        started = time.monotonic()
        try:
            if _hosted(env_type):
                await self._notify(
                    session_id,
                    "starting",
                    {
                        "tenant_id": tenant_id,
                        "cold": True,
                        "cause": cause,
                        "image": image,
                        "size": size,
                        "lock_wait_ms": lock_wait_ms,
                    },
                )
            with start_span(
                self.tracing,
                "sandbox.boot",
                session_id=session_id,
                lock_wait_ms=lock_wait_ms,
            ):
                optional: dict[str, Any] = {"web_search": True} if web_search else {}
                proc = await spawn_pi(
                    self.settings,
                    cwd=cwd,
                    tools=tools,
                    mcp_http=mcp_http,
                    function_tools=function_tools,
                    skill_dirs=skill_dirs,
                    model=model,
                    instructions=instructions,
                    api_key=api_key,
                    mem_mib=mem_mib,
                    image=image,
                    extra_env=extra_env,
                    thinking=level,
                    system_prompt=prompt,
                    system_prompt_set=True,
                    codemode=codemode,
                    env_type=env_type,
                    session_id=str(session_id),
                    **optional,
                )
        except Exception as exc:
            self._observe_boot(size, "error", time.monotonic() - started)
            self._observe_pi_spawn("error", env_type=env_type)
            await self._finish_spawn(session_id, error=exc)
            raise
        boot_ms = int((time.monotonic() - started) * 1000)
        self._observe_boot(size, "ok", time.monotonic() - started)
        self._observe_pi_spawn("ok", proc)
        log.info("pi ready", extra={"session_id": str(session_id)})
        async with self._lock:
            self._store_proc(
                session_id,
                proc,
                tools=tools,
                model=model,
                instructions=instructions,
                level=level,
                prompt=prompt,
                key_id=key_id,
                codemode=codemode,
                web_search=web_search,
                env_type=env_type,
                session_mem=session_mem,
                size=size,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                org_id=org_id,
                cause=cause,
                idle_ttl=idle_ttl,
                idle_ttl_set=idle_ttl_set,
            )
            done = self._inflight.pop(session_id, None)
            self._reserved.pop(session_id, None)
            self._booting.discard(session_id)
            if done is not None and not done.done():
                done.set_result(proc)
        if _hosted(env_type):
            image_id, image_version, _digest = self._image_fields(proc, env_type)
            setup_ms = getattr(proc, "setup_ms", None)
            await self._notify(
                session_id,
                "ready",
                {
                    "tenant_id": tenant_id,
                    "image": image_id or image,
                    "image_version": image_version,
                    "size": size,
                    "run_mode": self.settings.run_mode,
                    "boot_ms": boot_ms,
                    "lock_wait_ms": lock_wait_ms,
                    "setup_ms": setup_ms if isinstance(setup_ms, int) else 0,
                },
            )
        return proc

    def _same(
        self,
        session_id: uuid.UUID,
        *,
        tools: bool,
        model: str | None,
        instructions: str | None,
        level: str,
        prompt: str | None,
        key_id: str | None,
        codemode: str = "off",
        web_search: bool = False,
    ) -> bool:
        same = self._spawn_tools.get(session_id) == tools
        same = same and self._models.get(session_id) == model
        same = same and self._instructions.get(session_id) == instructions
        same = same and self._thinking.get(session_id) == level
        same = same and self._codemodes.get(session_id, "off") == codemode
        same = same and self._web_search.get(session_id, False) == web_search
        same = same and self._system_prompts.get(session_id) == prompt
        return same and self._key_ids.get(session_id) == key_id

    def _claim_or_reuse(
        self,
        session_id: uuid.UUID,
        *,
        tools: bool,
        model: str | None,
        instructions: str | None,
        level: str,
        prompt: str | None,
        key_id: str | None,
        tenant_id: uuid.UUID | None,
        session_mem: int,
        env_type: str | None,
        idle_ttl: timedelta | None,
        idle_ttl_set: bool,
        lock_wait_ms: int,
    ) -> PiProc | str:
        proc = self._procs.get(session_id)
        if proc is not None and proc.alive:
            with start_span(
                self.tracing,
                "sandbox.attach",
                session_id=session_id,
                lock_wait_ms=lock_wait_ms,
            ):
                if env_type is not None:
                    self._env_types[session_id] = env_type
                if idle_ttl_set:
                    self._ttls[session_id] = (
                        idle_ttl.total_seconds() if idle_ttl is not None else None
                    )
                self._last[session_id] = time.monotonic()
            return proc
        code = self.capacity_code(session_id, tenant_id, session_mem_mib=session_mem)
        if code is not None:
            message = (
                "Too many live sessions for this tenant"
                if code == "capacity_tenant"
                else "Too many live sessions"
            )
            log_event(
                log,
                logging.WARNING,
                "worker assign failed",
                event="worker.assign.failed",
                error_code=code,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            raise CapacityError(message, code=code)
        log.info(
            "pi spawn",
            extra={"session_id": str(session_id), "run_mode": self.settings.run_mode},
        )
        return "spawn"

    async def _finish_spawn(
        self, session_id: uuid.UUID, *, error: BaseException
    ) -> None:
        async with self._lock:
            self._reserved.pop(session_id, None)
            self._booting.discard(session_id)
            future = self._inflight.pop(session_id, None)
            if future is not None and not future.done():
                future.set_exception(error)
                future.add_done_callback(_consume_future)

    def _store_proc(
        self,
        session_id: uuid.UUID,
        proc: PiProc,
        *,
        tools: bool,
        model: str | None,
        instructions: str | None,
        level: str,
        prompt: str | None,
        key_id: str | None,
        codemode: str,
        web_search: bool,
        env_type: str | None,
        session_mem: int,
        size: str,
        tenant_id: uuid.UUID | None,
        agent_id: str | None,
        user_id: str | None,
        org_id: str | None,
        cause: str,
        idle_ttl: timedelta | None,
        idle_ttl_set: bool,
    ) -> None:
        self._procs[session_id] = proc
        self._spawn_tools[session_id] = tools
        self._models[session_id] = model
        self._instructions[session_id] = instructions
        self._thinking[session_id] = level
        self._codemodes[session_id] = codemode
        self._web_search[session_id] = web_search
        self._system_prompts[session_id] = prompt
        self._key_ids[session_id] = key_id
        self._env_types[session_id] = env_type
        self._mem[session_id] = session_mem
        self._sizes[session_id] = size
        born = time.monotonic()
        self._born[session_id] = born
        if tenant_id is not None:
            self._tenants[session_id] = tenant_id
        if idle_ttl_set:
            self._ttls[session_id] = (
                idle_ttl.total_seconds() if idle_ttl is not None else None
            )
        self._last[session_id] = time.monotonic()
        self._note_start(
            session_id,
            proc,
            cause=cause,
            born=born,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            org_id=org_id,
            key_id=key_id,
            env_type=env_type,
            size=size,
        )

    def live(self) -> int:
        return sum(1 for proc in self._procs.values() if proc.alive) + len(
            self._reserved
        )

    def live_procs(self) -> list[tuple[str, PiProc]]:
        return [
            (self._sizes.get(sid, "S"), proc)
            for sid, proc in self._procs.items()
            if proc.alive
        ]

    def live_for(self, tenant_id: uuid.UUID) -> int:
        running = sum(
            1
            for sid, proc in self._procs.items()
            if proc.alive and self._tenants.get(sid) == tenant_id
        )
        waiting = sum(1 for tid, _mem in self._reserved.values() if tid == tenant_id)
        return running + waiting

    def capacity_code(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
        session_mem_mib: int | None = None,
    ) -> str | None:
        proc = self._procs.get(session_id)
        if proc is not None and proc.alive:
            return None
        if (
            tenant_id is not None
            and self.live_for(tenant_id) >= self.settings.max_sessions_per_tenant
        ):
            return "capacity_tenant"
        if self.live() >= self.settings.max_sessions:
            return "capacity"
        incoming = (
            session_mem_mib
            if session_mem_mib is not None
            else self.settings.microvm_mem_mib
        )
        used = sum(
            self._mem.get(sid, self.settings.microvm_mem_mib)
            for sid, item in self._procs.items()
            if item.alive
        )
        used += sum(mem for _tid, mem in self._reserved.values())
        if used + incoming > self.settings.node_memory_mb():
            return "capacity"
        return None

    def has_capacity(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
        session_mem_mib: int | None = None,
    ) -> bool:
        return (
            self.capacity_code(session_id, tenant_id, session_mem_mib=session_mem_mib)
            is None
        )

    def peek(self, session_id: uuid.UUID) -> PiProc | None:
        proc = self._procs.get(session_id)
        if proc is None or not proc.alive:
            return None
        return proc

    def touch(self, session_id: uuid.UUID) -> None:
        self._last[session_id] = time.monotonic()

    def sandbox_seen_ids(self) -> list[uuid.UUID]:
        live = [sid for sid, proc in self._procs.items() if proc.alive]
        return list({*live, *self._booting})

    async def _notify(
        self, session_id: uuid.UUID, phase: str, fields: dict[str, Any]
    ) -> None:
        callback = self.on_transition
        if callback is None:
            return
        try:
            await callback(session_id, phase, fields)
        except Exception:
            log.exception(
                "sandbox transition failed", extra={"session_id": str(session_id)}
            )

    async def kill(self, session_id: uuid.UUID, *, reason: str = "session") -> None:
        live = self._live.pop(session_id, None)
        env_type = self._env_types.get(session_id)
        tenant_id = self._tenants.get(session_id)
        born_for_ms = self._born.get(session_id)
        proc = self._procs.pop(session_id, None)
        self._last.pop(session_id, None)
        self._spawn_tools.pop(session_id, None)
        self._models.pop(session_id, None)
        self._instructions.pop(session_id, None)
        self._thinking.pop(session_id, None)
        self._codemodes.pop(session_id, None)
        self._web_search.pop(session_id, None)
        self._system_prompts.pop(session_id, None)
        self._ttls.pop(session_id, None)
        self._key_ids.pop(session_id, None)
        self._env_types.pop(session_id, None)
        self._mem.pop(session_id, None)
        self._tenants.pop(session_id, None)
        size = self._sizes.pop(session_id, "S")
        born = self._born.pop(session_id, None)
        self._booting.discard(session_id)
        if live is not None:
            self._emit_stop(live, reason=reason, born=born)
        if _hosted(env_type):
            started = born_for_ms if born_for_ms is not None else born
            if isinstance(started, (int, float)):
                live_ms = int(max(0.0, time.monotonic() - started) * 1000)
            else:
                live_ms = 0
            await self._notify(
                session_id,
                "stopped",
                {
                    "tenant_id": tenant_id,
                    "reason": _EVENT_REASON.get(reason, reason),
                    "live_ms": live_ms,
                },
            )
        if proc is not None and self.metrics is not None:
            hold = time.monotonic() - born if born is not None else 0.0
            self.metrics.observe_sandbox_destroy(size=size, hold_seconds=hold)
            if proc.vm_id is None:
                self.metrics.observe_pi_kill(reason)
        if self.on_kill is not None:
            await self.on_kill(session_id, proc)
        if proc is not None:
            proc.stop_reason = reason
            await proc.terminate()

    def _observe_boot(self, size: str, result: str, seconds: float) -> None:
        if self.metrics is None:
            return
        self.metrics.observe_sandbox_boot(size=size, result=result, seconds=seconds)

    def _observe_pi_spawn(
        self,
        result: str,
        proc: PiProc | None = None,
        *,
        env_type: str | None = None,
    ) -> None:
        if self.metrics is None:
            return
        if result == "ok":
            if proc is None or proc.vm_id is not None:
                return
        elif env_type != "none":
            return
        self.metrics.observe_pi_spawn(result)

    def refresh_metrics(self) -> None:
        if self.metrics is None:
            return
        used = sum(
            self._mem.get(sid, self.settings.microvm_mem_mib)
            for sid, proc in self._procs.items()
            if proc.alive
        )
        self.metrics.set_worker_util(
            capacity=self.settings.max_sessions,
            sessions=self.live(),
            memory_mib_used=used,
            memory_mib_total=self.settings.node_memory_mb(),
        )
        counts = {"S": 0, "M": 0, "L": 0}
        for sid, proc in self._procs.items():
            if proc.alive:
                size = self._sizes.get(sid, "S")
                counts[size] = counts.get(size, 0) + 1
        self.metrics.set_sandboxes_active(counts)

    def hold(self, session_id: uuid.UUID) -> None:
        self._held.add(session_id)

    def release(self, session_id: uuid.UUID) -> None:
        self._held.discard(session_id)

    def held(self, session_id: uuid.UUID) -> bool:
        return session_id in self._held

    def alive(self, session_id: uuid.UUID) -> bool:
        proc = self._procs.get(session_id)
        return proc is not None and proc.alive

    def _ttl_seconds(self, session_id: uuid.UUID) -> float | None:
        if session_id in self._ttls:
            return self._ttls[session_id]
        ttl = self.settings.pi_idle_ttl_for(self._env_types.get(session_id))
        if ttl is None:
            return None
        return ttl.total_seconds()

    def live_entries(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sid, proc in self._procs.items():
            if not proc.alive:
                continue
            live = self._live.get(sid)
            if live is None:
                continue
            rows.append({key: value for key, value in live.items() if key != "born"})
        return rows

    async def sweep_dead(self) -> None:
        dead = [sid for sid, proc in list(self._procs.items()) if not proc.alive]
        for sid in dead:
            await self.kill(sid, reason="crash")

    def _note_start(
        self,
        session_id: uuid.UUID,
        proc: PiProc,
        *,
        cause: str,
        born: float,
        tenant_id: uuid.UUID | None,
        agent_id: str | None,
        user_id: str | None,
        org_id: str | None,
        key_id: str | None,
        env_type: str | None,
        size: str,
    ) -> None:
        emitter = self.lifecycle
        if emitter is None or not emitter.active:
            return
        image_id, image_version, image_digest = self._image_fields(proc, env_type)
        fields = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "org_id": org_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "key_id": key_id,
            "environment_type": env_type,
            "sandbox_size": size,
            "sandbox_image": image_id,
            "image_version": image_version,
            "image_digest": image_digest,
            "run_mode": self.settings.run_mode,
            "born": born,
            "started_at": utc_ts(),
        }
        seq = emitter.emit_start(fields, cause=cause)
        if seq is None:
            return
        fields["start_seq"] = seq
        self._live[session_id] = fields

    def _emit_stop(
        self, live: dict[str, Any], *, reason: str, born: float | None
    ) -> None:
        emitter = self.lifecycle
        if emitter is None or not emitter.active:
            return
        started = born if born is not None else live.get("born")
        if isinstance(started, (int, float)):
            live_ms = int(max(0.0, time.monotonic() - started) * 1000)
        else:
            live_ms = 0
        event_reason = _EVENT_REASON.get(reason, reason)
        emitter.emit_stop(live, reason=event_reason, live_ms=live_ms)

    def _image_fields(
        self, proc: PiProc, env_type: str | None
    ) -> tuple[str | None, str | None, str | None]:
        if self.settings.run_mode != "microvm" or env_type == "none":
            return None, None, None
        image = getattr(proc, "image", None)
        if image is None:
            return None, None, None
        image_id = getattr(image, "id", None)
        version = getattr(image, "version", None)
        digest = getattr(image, "digest", None)
        return (
            image_id if isinstance(image_id, str) else None,
            version if isinstance(version, str) else None,
            digest if isinstance(digest, str) else None,
        )

    async def reap(self) -> None:
        now = time.monotonic()
        idle = [
            sid
            for sid, last in self._last.items()
            if (ttl := self._ttl_seconds(sid)) is not None and now - last >= ttl
        ]
        for sid in idle:
            await self.kill(sid, reason="idle")

    async def reap_loop(self) -> None:
        seconds = [
            ttl.total_seconds()
            for ttl in (self.settings.idle_ttl, self.settings.sandbox_ttl_openai_hosted)
            if ttl is not None
        ]
        base = min(seconds) if seconds else 15.0
        interval = min(1.0, max(0.02, base / 5))
        while True:
            await asyncio.sleep(interval)
            await self.reap()

    async def enforce_memory(self) -> None:
        limit = self.settings.pi_mem_mib
        if limit is None:
            return
        from apipi.worker.procmem import read_group_rss_pss

        ceiling = limit * 1024 * 1024
        for sid, proc in list(self._procs.items()):
            if not proc.alive or proc.vm_id:
                continue
            process = getattr(proc, "process", None)
            pid = getattr(process, "pid", None)
            if pid is None:
                continue
            rss, _pss = read_group_rss_pss(pid)
            if rss <= ceiling:
                continue
            log.info(
                "pi memory",
                extra={
                    "session_id": str(sid),
                    "rss": rss,
                    "limit_mib": limit,
                },
            )
            await self.kill(sid, reason="memory")

    async def kill_unheld(self, *, reason: str = "idle") -> None:
        for sid in list(self._procs):
            if self.alive(sid) and not self.held(sid):
                await self.kill(sid, reason=reason)

    async def close(self) -> None:
        for sid in list(self._procs):
            await self.kill(sid, reason="shutdown")
