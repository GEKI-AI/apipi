import asyncio
import time
import uuid

from apipi.config import CapacityError, Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer, stop_mcp_stdio
from apipi.pi.proc import PiProc, spawn_pi


class PiPool:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._procs: dict[uuid.UUID, PiProc] = {}
        self._stdio: dict[uuid.UUID, list[McpStdioServer]] = {}
        self._last: dict[uuid.UUID, float] = {}
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
    ) -> PiProc:
        async with self._lock:
            proc = self._procs.get(session_id)
            if proc is None or not proc.alive:
                if not self.has_capacity(session_id):
                    raise CapacityError("Too many live sessions")
                proc = await spawn_pi(
                    self.settings,
                    cwd=cwd,
                    tools=tools,
                    mcp_http=mcp_http,
                    mcp_stdio=mcp_stdio,
                    skill_dirs=skill_dirs,
                )
                self._procs[session_id] = proc
            self._last[session_id] = time.monotonic()
            return proc

    def live(self) -> int:
        return sum(1 for proc in self._procs.values() if proc.alive)

    def has_capacity(self, session_id: uuid.UUID) -> bool:
        proc = self._procs.get(session_id)
        if proc is not None and proc.alive:
            return True
        return self.live() < self.settings.max_sessions

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
        stdio = self._stdio.pop(session_id, None)
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
