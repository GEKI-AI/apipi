import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from apipi.config import CapacityError, Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer, stop_mcp_stdio
from apipi.pi.proc import PiProc, spawn_pi

OnKill = Callable[[uuid.UUID, PiProc | None], Awaitable[None]]


class PiPool:
    def __init__(self, settings: Settings, *, on_kill: OnKill | None = None) -> None:
        self.settings = settings
        self.on_kill = on_kill
        self._procs: dict[uuid.UUID, PiProc] = {}
        self._tenants: dict[uuid.UUID, uuid.UUID] = {}
        self._stdio: dict[uuid.UUID, list[McpStdioServer]] = {}
        self._last: dict[uuid.UUID, float] = {}
        self._spawn_tools: dict[uuid.UUID, bool] = {}
        self._models: dict[uuid.UUID, str | None] = {}
        self._instructions: dict[uuid.UUID, str | None] = {}
        self._key_ids: dict[uuid.UUID, str | None] = {}
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
    ) -> PiProc:
        instructions = instructions if instructions else None
        async with self._lock:
            proc = self._procs.get(session_id)
            spawned = self._spawn_tools.get(session_id)
            same = spawned == tools and self._models.get(session_id) == model
            same = same and self._instructions.get(session_id) == instructions
            same = same and self._key_ids.get(session_id) == key_id
            if proc is not None and proc.alive and not same:
                await self.kill(session_id)
                proc = None
            if proc is None or not proc.alive:
                code = self.capacity_code(session_id, tenant_id)
                if code is not None:
                    message = (
                        "Too many live sessions for this tenant"
                        if code == "capacity_tenant"
                        else "Too many live sessions"
                    )
                    raise CapacityError(message, code=code)
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
                )
                self._procs[session_id] = proc
                self._spawn_tools[session_id] = tools
                self._models[session_id] = model
                self._instructions[session_id] = instructions
                self._key_ids[session_id] = key_id
                if tenant_id is not None:
                    self._tenants[session_id] = tenant_id
            self._last[session_id] = time.monotonic()
            return proc

    def live(self) -> int:
        return sum(1 for proc in self._procs.values() if proc.alive)

    def live_for(self, tenant_id: uuid.UUID) -> int:
        return sum(
            1
            for sid, proc in self._procs.items()
            if proc.alive and self._tenants.get(sid) == tenant_id
        )

    def capacity_code(
        self, session_id: uuid.UUID, tenant_id: uuid.UUID | None = None
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
        return None

    def has_capacity(
        self, session_id: uuid.UUID, tenant_id: uuid.UUID | None = None
    ) -> bool:
        return self.capacity_code(session_id, tenant_id) is None

    def peek(self, session_id: uuid.UUID) -> PiProc | None:
        proc = self._procs.get(session_id)
        if proc is None or not proc.alive:
            return None
        return proc

    def touch(self, session_id: uuid.UUID) -> None:
        self._last[session_id] = time.monotonic()

    def put_stdio(self, session_id: uuid.UUID, servers: list[McpStdioServer]) -> None:
        self._stdio[session_id] = servers

    async def kill(self, session_id: uuid.UUID) -> None:
        proc = self._procs.pop(session_id, None)
        self._last.pop(session_id, None)
        self._spawn_tools.pop(session_id, None)
        self._models.pop(session_id, None)
        self._instructions.pop(session_id, None)
        self._key_ids.pop(session_id, None)
        self._tenants.pop(session_id, None)
        stdio = self._stdio.pop(session_id, None)
        if self.on_kill is not None:
            await self.on_kill(session_id, proc)
        if proc is not None:
            await proc.terminate()
        if stdio:
            await stop_mcp_stdio(stdio)

    def alive(self, session_id: uuid.UUID) -> bool:
        proc = self._procs.get(session_id)
        return proc is not None and proc.alive

    async def reap(self) -> None:
        ttl = self.settings.idle_ttl.total_seconds()
        now = time.monotonic()
        idle = [sid for sid, last in self._last.items() if now - last >= ttl]
        for sid in idle:
            await self.kill(sid)

    async def reap_loop(self) -> None:
        interval = min(1.0, max(0.02, self.settings.idle_ttl.total_seconds() / 5))
        while True:
            await asyncio.sleep(interval)
            await self.reap()

    async def close(self) -> None:
        for sid in list(self._procs):
            await self.kill(sid)
