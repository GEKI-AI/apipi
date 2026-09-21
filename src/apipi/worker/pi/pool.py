import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from apipi.config import CapacityError, Settings
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing, start_span
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer, stop_mcp_stdio
from apipi.worker.pi.proc import PiProc, spawn_pi
from apipi.worker.pi.sandbox import size_for_mem

OnKill = Callable[[uuid.UUID, PiProc | None], Awaitable[None]]
log = logging.getLogger("apipi.worker.pi")


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
        self._stdio: dict[uuid.UUID, list[McpStdioServer]] = {}
        self._last: dict[uuid.UUID, float] = {}
        self._spawn_tools: dict[uuid.UUID, bool] = {}
        self._models: dict[uuid.UUID, str | None] = {}
        self._instructions: dict[uuid.UUID, str | None] = {}
        self._key_ids: dict[uuid.UUID, str | None] = {}
        self._env_types: dict[uuid.UUID, str | None] = {}
        self._mem: dict[uuid.UUID, int] = {}
        self._sizes: dict[uuid.UUID, str] = {}
        self._born: dict[uuid.UUID, float] = {}
        self._held: set[uuid.UUID] = set()
        self._lock = asyncio.Lock()

    async def get(
        self,
        session_id: uuid.UUID,
        *,
        cwd: str | None,
        tools: bool,
        mcp_http: list[McpHttpServer] | None = None,
        mcp_stdio: list[McpStdioServer] | None = None,
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
    ) -> PiProc:
        instructions = instructions if instructions else None
        session_mem = mem_mib if mem_mib is not None else self.settings.microvm_mem_mib
        async with self._lock:
            proc = self._procs.get(session_id)
            spawned = self._spawn_tools.get(session_id)
            same = spawned == tools and self._models.get(session_id) == model
            same = same and self._instructions.get(session_id) == instructions
            same = same and self._key_ids.get(session_id) == key_id
            if proc is not None and proc.alive and not same:
                await self.kill(session_id, reason="respawn")
                proc = None
            reused = proc is not None and proc.alive
            with start_span(
                self.tracing,
                "sandbox.attach" if reused else "sandbox.boot",
                session_id=session_id,
            ):
                if proc is None or not proc.alive:
                    code = self.capacity_code(
                        session_id, tenant_id, session_mem_mib=session_mem
                    )
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
                        extra={
                            "session_id": str(session_id),
                            "run_mode": self.settings.run_mode,
                        },
                    )
                    size = size_for_mem(self.settings, session_mem)
                    started = time.monotonic()
                    try:
                        proc = await spawn_pi(
                            self.settings,
                            cwd=cwd,
                            tools=tools,
                            mcp_http=mcp_http,
                            mcp_stdio=mcp_stdio,
                            skill_dirs=skill_dirs,
                            model=model,
                            instructions=instructions,
                            api_key=api_key,
                            mem_mib=mem_mib,
                            image=image,
                            extra_env=extra_env,
                        )
                    except Exception:
                        self._observe_boot(size, "error", time.monotonic() - started)
                        self._observe_pi_spawn("error")
                        raise
                    self._observe_boot(size, "ok", time.monotonic() - started)
                    self._observe_pi_spawn("ok", proc)
                    log.info("pi ready", extra={"session_id": str(session_id)})
                    self._procs[session_id] = proc
                    self._spawn_tools[session_id] = tools
                    self._models[session_id] = model
                    self._instructions[session_id] = instructions
                    self._key_ids[session_id] = key_id
                    self._env_types[session_id] = env_type
                    self._mem[session_id] = session_mem
                    self._sizes[session_id] = size
                    self._born[session_id] = time.monotonic()
                    if tenant_id is not None:
                        self._tenants[session_id] = tenant_id
                elif env_type is not None:
                    self._env_types[session_id] = env_type
                self._last[session_id] = time.monotonic()
                return proc

    def live(self) -> int:
        return sum(1 for proc in self._procs.values() if proc.alive)

    def live_procs(self) -> list[tuple[str, PiProc]]:
        return [
            (self._sizes.get(sid, "S"), proc)
            for sid, proc in self._procs.items()
            if proc.alive
        ]

    def live_for(self, tenant_id: uuid.UUID) -> int:
        return sum(
            1
            for sid, proc in self._procs.items()
            if proc.alive and self._tenants.get(sid) == tenant_id
        )

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

    def put_stdio(self, session_id: uuid.UUID, servers: list[McpStdioServer]) -> None:
        self._stdio[session_id] = servers

    async def kill(self, session_id: uuid.UUID, *, reason: str = "session") -> None:
        proc = self._procs.pop(session_id, None)
        self._last.pop(session_id, None)
        self._spawn_tools.pop(session_id, None)
        self._models.pop(session_id, None)
        self._instructions.pop(session_id, None)
        self._key_ids.pop(session_id, None)
        self._env_types.pop(session_id, None)
        self._mem.pop(session_id, None)
        self._tenants.pop(session_id, None)
        size = self._sizes.pop(session_id, "S")
        born = self._born.pop(session_id, None)
        stdio = self._stdio.pop(session_id, None)
        if proc is not None and self.metrics is not None:
            hold = time.monotonic() - born if born is not None else 0.0
            self.metrics.observe_sandbox_destroy(size=size, hold_seconds=hold)
            if proc.vm_id is None:
                self.metrics.observe_pi_kill(reason)
        if self.on_kill is not None:
            await self.on_kill(session_id, proc)
        if proc is not None:
            await proc.terminate()
        if stdio:
            await stop_mcp_stdio(stdio)

    def _observe_boot(self, size: str, result: str, seconds: float) -> None:
        if self.metrics is None:
            return
        self.metrics.observe_sandbox_boot(size=size, result=result, seconds=seconds)

    def _observe_pi_spawn(self, result: str, proc: PiProc | None = None) -> None:
        if self.metrics is None:
            return
        if result == "ok":
            if proc is None or proc.vm_id is not None:
                return
        elif not self._host_backend():
            return
        self.metrics.observe_pi_spawn(result)

    def _host_backend(self) -> bool:
        from apipi.worker.pi.isolation import load_isolation

        return load_isolation(self.settings.run_mode).stdio_on_host

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
        ttl = self.settings.pi_idle_ttl_for(self._env_types.get(session_id))
        if ttl is None:
            return None
        return ttl.total_seconds()

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
            for ttl in (self.settings.idle_ttl, self.settings.workspace_ttl)
            if ttl is not None
        ]
        base = min(seconds) if seconds else 15.0
        interval = min(1.0, max(0.02, base / 5))
        while True:
            await asyncio.sleep(interval)
            await self.reap()

    async def kill_unheld(self, *, reason: str = "idle") -> None:
        for sid in list(self._procs):
            if self.alive(sid) and not self.held(sid):
                await self.kill(sid, reason=reason)

    async def close(self) -> None:
        for sid in list(self._procs):
            await self.kill(sid, reason="shutdown")
